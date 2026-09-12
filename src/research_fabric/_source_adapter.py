"""Source adapter contract and built-in text implementations."""

from __future__ import annotations

import html
import pathlib
import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


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


_MARKDOWN = MarkdownIt("commonmark")


def _image_targets(text: str) -> list[str]:
    targets = []
    for token in _MARKDOWN.parse(text):
        for child in token.children or ():
            if child.type == "image" and isinstance(src := child.attrGet("src"), str):
                targets.append(src)
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
        local = (_local_asset(target) for target in _image_targets(decoded))
        return tuple(dict.fromkeys(asset for asset in local if asset))

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        return {"content_type": "text/markdown", "filename": path.name}


ADAPTERS: tuple[SourceAdapter, ...] = (HTMLAdapter(), MarkdownAdapter())
