"""Source adapter contract and built-in text implementations."""

from __future__ import annotations

import html
import pathlib
import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import unquote, urlsplit


class SourceAdapter(Protocol):
    """Minimal contract every source format must implement."""

    name: str
    version: str
    suffixes: tuple[str, ...]

    def decode(self, raw: bytes) -> str: ...

    def extract_text(self, decoded: str) -> str: ...

    def map_locator(self, locator: str) -> str: ...

    def assets(self, decoded: str) -> tuple[str, ...]: ...

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


_MD_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_MD_IMAGE = re.compile(r"(?<!\\)!\[([^\]]*)\]")
_MD_REF_DEF = re.compile(r"(?m)^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))")


def _closes_fence(match, fence: str) -> bool:
    if not match:
        return False
    marker, tail = match.groups()
    return marker[0] == fence[0] and len(marker) >= len(fence) and not tail.strip()


def _markdown_outside_fences(text: str) -> str:
    out, fence = [], None
    for line in text.splitlines():
        match = _MD_FENCE.match(line)
        if fence:
            if _closes_fence(match, fence):
                fence = None
            continue
        if match:
            fence = match.group(1)
            continue
        out.append(line)
    return "\n".join(out)


def _bare_destination(text: str) -> str | None:
    depth = 0
    chars = iter(enumerate(text))
    for pos, char in chars:
        if char == "\\":
            next(chars, None)
        elif char == "(":
            depth += 1
        elif char == ")":
            if not depth:
                return text[:pos]
            depth -= 1
        elif char.isspace() and not depth:
            return text[:pos]
    return None


def _inline_destination(tail: str) -> str | None:
    body = tail[1:].lstrip()
    if body.startswith("<"):
        end = body.find(">")
        return body[1:end] if end >= 0 else None
    return _bare_destination(body)


def _label(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _reference_destination(alt: str, tail: str, definitions: dict[str, str]) -> str | None:
    if not tail.startswith("["):
        return definitions.get(_label(alt))
    end = tail.find("]")
    if end < 0:
        return None
    return definitions.get(_label(tail[1:end] or alt))


def _image_targets(text: str) -> list[str]:
    definitions = {
        _label(label): target1 or target2 for label, target1, target2 in _MD_REF_DEF.findall(text)
    }
    targets = []
    for match in _MD_IMAGE.finditer(text):
        tail = text[match.end() :]
        target = (
            _inline_destination(tail)
            if tail.startswith("(")
            else _reference_destination(match.group(1), tail, definitions)
        )
        if target:
            targets.append(target)
    return targets


def _local_asset(target: str) -> str | None:
    target = re.sub(r"\\([\\() ])", r"\1", target)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    return unquote(parsed.path) or None


class MarkdownAdapter:
    name = "markdown"
    version = "1"
    suffixes = (".md", ".markdown")

    def decode(self, raw: bytes) -> str:
        return raw.decode("utf-8")

    def extract_text(self, decoded: str) -> str:
        return decoded.replace("\r\n", "\n").replace("\r", "\n")

    def map_locator(self, locator: str) -> str:
        return locator.strip()

    def assets(self, decoded: str) -> tuple[str, ...]:
        targets = _image_targets(_markdown_outside_fences(decoded))
        local = (_local_asset(target) for target in targets)
        return tuple(dict.fromkeys(asset for asset in local if asset))

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        return {"content_type": "text/markdown", "filename": path.name}


ADAPTERS: tuple[SourceAdapter, ...] = (HTMLAdapter(), MarkdownAdapter())
