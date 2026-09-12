"""Format-independent source loading and provenance."""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

from ._source_adapter import ADAPTERS, SourceAdapter

# A source is trusted only when both the original bytes and worker representation match.


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
            raise RuntimeError(f"sha256 mismatch: {source.name}: {row.get('sha256')} != {rep.original_sha256}")
        enriched = dict(row)
        enriched.update(
            adapter=rep.adapter,
            adapter_version=rep.adapter_version,
            representation_encoding="utf-8",
            representation_sha256=rep.representation_sha256,
        )
        bound.append(enriched)
    return bound
