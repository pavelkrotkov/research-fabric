"""Source adapter contract and built-in text implementations."""

from __future__ import annotations

import html
import pathlib
import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import html_inline, image

from ._epub_adapter import EPUBAdapter


class SourceAdapter(Protocol):
    """Minimal contract every source format must implement."""

    name: str
    version: str
    suffixes: tuple[str, ...]

    def decode(self, raw: bytes) -> str | bytes: ...

    def extract_text(self, decoded: str | bytes) -> str: ...

    def map_locator(self, locator: str) -> str: ...

    def assets(self, decoded: str | bytes) -> tuple[str, ...]: ...

    def metadata(self, path: pathlib.Path) -> dict[str, str]: ...


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class HTMLAdapter:
    name = "html"
    version = "1"
    suffixes = (".html", ".htm")

    def decode(self, raw: bytes) -> str:
        return raw.decode("utf-8")

    def extract_text(self, decoded: str) -> str:
        parser = _HTMLText()
        parser.feed(decoded)
        text = html.unescape("".join(parser.parts))
        text = re.sub(r"[ \t]+", " ", text)
        return re.sub(r"\n\s*\n+", "\n\n", text).strip()

    def map_locator(self, locator: str) -> str:
        return locator.strip()

    def assets(self, decoded: str) -> tuple[str, ...]:
        return ()

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        return {"content_type": "text/html", "filename": path.name}


_NATIVE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _lines(text):
    # CommonMark lines end at CR/LF, not at Unicode paragraph separators.
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)[:-1]


def _positioned(rule):
    def parse(state, silent):
        start = state.pos
        matched = rule(state, silent)
        if matched and not silent:
            state.tokens[-1].meta["source_span"] = (start, state.pos)
        return matched

    return parse


def _source_positions(content, lines, start):
    """Map parser-stripped container prefixes back to original character offsets."""
    positions = []
    for fragment, line in zip(_lines(content), lines):
        body = fragment.rstrip("\r\n")
        column = line.find(body)
        if column < 0:
            raise ValueError("cannot map visual reference to original source")
        positions.extend(range(start + column, start + column + len(body)))
        if fragment.endswith("\n"):
            positions.append(start + len(line) - 1)
        start += len(line)
    if len(positions) != len(content):
        raise ValueError("incomplete visual source position mapping")
    return positions


_MARKDOWN = MarkdownIt("commonmark")
_MARKDOWN.inline.ruler.at("image", _positioned(image))
_MARKDOWN.inline.ruler.at("html_inline", _positioned(html_inline))


class _VisualHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.inert = 0
        self._edits = None
        self._objects = []

    def visual_edits(self, text):
        """Return active visual tag spans without a second HTML policy."""
        self.reset()
        self.references = []
        self._edits = []
        self._text = text
        self._starts = [0, *(match.end() for match in re.finditer("\n", text))]
        self.feed(text)
        return self._edits

    def _replace(self, raw, replacement):
        line, column = self.getpos()
        start = self._starts[line - 1] + column
        self._edits.append((start, start + len(raw), replacement))

    def _track_starttag(self, tag, visual):
        if self._edits is None:
            return
        if tag == "object":
            self._objects.append(visual)
        if visual:
            self._replace(self.get_starttag_text(), "[Figure]")

    def handle_starttag(self, tag, attrs):
        before = len(self.references)
        if tag in ("pre", "code", "script", "style"):
            self.inert += 1
        if not self.inert:
            attrs = dict(attrs)
            if tag in ("img", "embed", "object"):
                target = attrs.get("data" if tag == "object" else "src")
                if target:
                    self.references.append(
                        {
                            "target": target,
                            "syntax": tag,
                            "caption": attrs.get("alt", attrs.get("title", "")),
                            "required": attrs.get("data-optional") != "true",
                        }
                    )
                if "srcset" in attrs:
                    self.references.append(
                        {
                            "target": attrs["srcset"],
                            "syntax": "srcset",
                            "required": attrs.get("data-optional") != "true",
                        }
                    )
        self._track_starttag(tag, len(self.references) > before)

    def handle_endtag(self, tag):
        if tag in ("pre", "code", "script", "style"):
            self.inert = max(0, self.inert - 1)
        if self._edits is not None and tag == "object" and self._objects and self._objects.pop():
            line, column = self.getpos()
            start = self._starts[line - 1] + column
            self._replace(self._text[start : self._text.index(">", start) + 1], "")


def _token_visual(child, parser):
    if child.type in ("html_inline", "html_block"):
        edits = parser.visual_edits(child.content)
        if child.type == "html_inline":
            start = child.meta["source_span"][0]
            edits = [(start + a, start + b, value) for a, b, value in edits]
        return parser.references, edits
    if child.type == "image" and not parser.inert:
        row = {"target": child.attrGet("src"), "syntax": "image", "caption": child.content, "required": True}
        return [row], [(*child.meta["source_span"], "[Figure]")]
    return [], []


def _block_visual(token, parser):
    locator = {"lines": [n + 1 for n in token.map]} if token.map else {}
    rows = []
    edits = []
    for child in token.children or [token]:
        child_rows, child_edits = _token_visual(child, parser)
        rows.extend({**row, "locator": locator} for row in child_rows)
        edits.extend(child_edits)
    return rows, edits


def _source_edits(token, edits, lines):
    if not edits:
        return []
    first, last = token.map
    positions = _source_positions(token.content, lines[first:last], sum(map(len, lines[:first])))
    return [(positions[a], positions[b - 1] + 1, value) for a, b, value in edits]


def _visual_source(text: str) -> tuple[list[dict], str]:
    """Discover and rewrite references in one CommonMark pass."""
    result = []
    edits = []
    lines = _lines(text)
    parser = _VisualHTML()
    for token in _MARKDOWN.parse(text):
        rows, local = _block_visual(token, parser)
        result.extend(rows)
        edits.extend(_source_edits(token, local, lines))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    text = _NATIVE_IMAGE.sub(lambda match: "&#33;" + match.group()[1:], text)
    return result, text


def visual_references(text: str) -> list[dict]:
    """Discover active images; locators retain one-based, end-exclusive block lines."""
    return _visual_source(text)[0]


def literal_caption(caption):
    """Literal captions keep scientific notation and cannot activate HTML tags."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", caption)), default=0))
    caption = _NATIVE_IMAGE.sub(lambda match: "&#33;" + match.group()[1:], caption)
    return f"{fence}text\n{caption}\n{fence}"


def _local_asset(target: str) -> str | None:
    target = re.sub(r"\\([\\() ])", r"\1", target)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    return unquote(parsed.path) or None


class MarkdownAdapter:
    name = "markdown"
    version = "2"
    suffixes = (".md", ".markdown")

    def decode(self, raw: bytes) -> str:
        return raw.decode("utf-8")

    def extract_text(self, decoded: str) -> str:
        return decoded.replace("\r\n", "\n").replace("\r", "\n")

    def map_locator(self, locator: str) -> str:
        return locator.strip()

    def assets(self, decoded: str) -> tuple[str, ...]:
        references = visual_references(decoded)
        local = (_local_asset(row["target"]) for row in references if row["syntax"] != "srcset")
        return tuple(dict.fromkeys(asset for asset in local if asset))

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        return {"content_type": "text/markdown", "filename": path.name}


ADAPTERS: tuple[SourceAdapter, ...] = (HTMLAdapter(), MarkdownAdapter(), EPUBAdapter())
