"""Format-independent source loading and provenance."""

from __future__ import annotations

import hashlib
import html
import pathlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol


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


@dataclass(frozen=True)
class SourceRepresentation:
    path: pathlib.Path
    adapter: str
    adapter_version: str
    text: str
    original_sha256: str
    representation_sha256: str


def adapter_for(path: pathlib.Path, adapters: tuple[SourceAdapter, ...] = ADAPTERS) -> SourceAdapter:
    matches = [adapter for adapter in adapters if path.suffix.lower() in adapter.suffixes]
    if len(matches) != 1:
        detail = "unsupported" if not matches else "ambiguous"
        raise ValueError(f"{detail} source format: {path.name}")
    return matches[0]


def representation_for(path: pathlib.Path, adapters: tuple[SourceAdapter, ...] = ADAPTERS) -> SourceRepresentation:
    raw = path.read_bytes()
    adapter = adapter_for(path, adapters)
    text = adapter.extract_text(adapter.decode(raw))
    return SourceRepresentation(
        path=path,
        adapter=adapter.name,
        adapter_version=adapter.version,
        text=text,
        original_sha256=hashlib.sha256(raw).hexdigest(),
        representation_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def discover_sources(source_dir: pathlib.Path, adapters: tuple[SourceAdapter, ...] = ADAPTERS) -> list[pathlib.Path]:
    """Return supported sources deterministically; reject same-stem ambiguity."""
    found = []
    seen = set()
    for path in sorted((p for p in source_dir.iterdir() if p.is_file()), key=lambda p: p.name):
        try:
            adapter_for(path, adapters)
        except ValueError as exc:
            if str(exc).startswith("unsupported"):
                continue
            raise
        key = path.stem.casefold()
        if key in seen:
            raise RuntimeError(f"ambiguous duplicate source input: {path.stem}")
        seen.add(key)
        found.append(path)
    return found


def bind_manifest(source_files: list[pathlib.Path], rows: list[dict]) -> list[dict]:
    """Verify original bytes and add exact worker-representation provenance."""
    by_name = {}
    for row in rows:
        name = pathlib.Path(row.get("snapshot", "")).name
        if not name:
            raise RuntimeError("source manifest row missing snapshot")
        if name in by_name:
            raise RuntimeError(f"duplicate source manifest entry: {name}")
        by_name[name] = row
    bound = []
    for source in source_files:
        row = by_name.get(source.name)
        if row is None:
            raise RuntimeError(f"source manifest has no entry for {source.name}")
        rep = representation_for(source)
        if rep.original_sha256 != row.get("sha256"):
            raise RuntimeError(f"sha256 mismatch for {source.name}: manifest={row.get('sha256')} actual={rep.original_sha256}")
        enriched = dict(row)
        enriched.update(
            adapter=rep.adapter,
            adapter_version=rep.adapter_version,
            representation_encoding="utf-8",
            representation_sha256=rep.representation_sha256,
        )
        bound.append(enriched)
    return bound
