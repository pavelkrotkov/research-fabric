"""Source adapter contract and built-in HTML implementation."""

from __future__ import annotations

import html
import pathlib
import re
from html.parser import HTMLParser
from typing import Protocol

# Adapter output is the representation hashed and consumed by every downstream gate.


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


ADAPTERS: tuple[SourceAdapter, ...] = (HTMLAdapter(),)
