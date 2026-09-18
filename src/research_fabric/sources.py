"""Format-independent source loading and provenance."""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

from ._source_adapter import ADAPTERS, SourceAdapter

REPRESENTATION_FIELDS = frozenset({"adapter", "adapter_version", "representation_encoding", "representation_sha256"})
ASSET_HASH_FIELD = "assets_sha256"


@dataclass(frozen=True)
class SourceRepresentation:
    path: pathlib.Path
    adapter: str
    adapter_version: str
    text: str
    original_sha256: str
    representation_sha256: str
    assets: tuple[str, ...] = ()
    assets_sha256: str | None = None


def source_attestation(rep: SourceRepresentation) -> dict[str, str]:
    """Return the exact source input attestation consumed by a worker."""
    return {
        "source_file": rep.path.name,
        "sha256": rep.original_sha256,
        **_representation_metadata(rep),
    }


def adapter_for(path: pathlib.Path, adapters: tuple[SourceAdapter, ...] = ADAPTERS) -> SourceAdapter:
    matches = [adapter for adapter in adapters if path.suffix.lower() in adapter.suffixes]
    if len(matches) != 1:
        detail = "unsupported" if not matches else "ambiguous"
        raise ValueError(f"{detail} source format: {path.name}")
    return matches[0]


def _asset_path(source: pathlib.Path, relative: str) -> pathlib.Path:
    path = pathlib.PurePosixPath(relative)
    root = source.parent.resolve()
    candidate = root.joinpath(*path.parts).resolve()
    if path.is_absolute() or ".." in path.parts or candidate == root or root not in candidate.parents:
        raise ValueError(f"unsafe source asset path in {source.name}: {relative}")
    return candidate


def _assets_sha256(source: pathlib.Path, assets: tuple[str, ...]) -> str | None:
    if not assets:
        return None
    digest = hashlib.sha256()
    for asset in assets:
        path = _asset_path(source, asset)
        if not path.is_file():
            raise FileNotFoundError(f"missing source asset for {source.name}: {asset}")
        digest.update(asset.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def representation_for(path: pathlib.Path, adapters: tuple[SourceAdapter, ...] = ADAPTERS) -> SourceRepresentation:
    raw = path.read_bytes()
    adapter = adapter_for(path, adapters)
    decoded = adapter.decode(raw)
    text = adapter.extract_text(decoded)
    assets = adapter.assets(decoded)
    return SourceRepresentation(
        path=path,
        adapter=adapter.name,
        adapter_version=adapter.version,
        text=text,
        original_sha256=hashlib.sha256(raw).hexdigest(),
        representation_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        assets=assets,
        assets_sha256=_assets_sha256(path, assets),
    )


def source_bundle(path: pathlib.Path) -> tuple[tuple[pathlib.Path, pathlib.Path], ...]:
    """Return source + local referenced assets as (input, relative-output) pairs."""
    rep = representation_for(path)
    assets = tuple((_asset_path(path, asset), pathlib.Path(asset)) for asset in rep.assets)
    return ((path, pathlib.Path(path.name)), *assets)


def source_provenance(
    source_files: list[pathlib.Path], adapters: tuple[SourceAdapter, ...] = ADAPTERS
) -> list[dict[str, str]]:
    """Hash and describe the representations supplied to one worker."""
    return sorted(
        (source_attestation(representation_for(path, adapters)) for path in source_files),
        key=lambda row: row["source_file"],
    )


def source_provenance_errors(actual, expected: list[dict[str, str]]) -> list[str]:
    """Return fail-closed errors for a packet's source input attestation."""
    if not isinstance(actual, list):
        return ["packet source provenance missing"]
    if any(not isinstance(row, dict) for row in actual):
        return ["packet source provenance contains a non-object entry"]
    actual_rows = sorted(actual, key=lambda row: str(row.get("source_file", "")))
    expected_rows = sorted(expected, key=lambda row: row["source_file"])
    if len(actual_rows) != len(expected_rows):
        return ["packet source provenance input count mismatch"]
    for actual_row, expected_row in zip(actual_rows, expected_rows):
        if actual_row != expected_row:
            return [f"packet source provenance mismatch for {expected_row['source_file']}"]
    return []


def packet_source_defects(packet: dict, expected: list[dict[str, str]], validator) -> list[str]:
    """Combine packet-shape and source-attestation checks at the worker boundary."""
    defects = list(validator(packet.get("parsed"))) if isinstance(packet, dict) else ["packet is not an object"]
    if isinstance(packet, dict):
        defects.extend(source_provenance_errors(packet.get("source_provenance"), expected))
    return defects


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


def _representation_metadata(rep: SourceRepresentation) -> dict[str, str]:
    metadata = {
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
    }
    if rep.assets_sha256:
        metadata[ASSET_HASH_FIELD] = rep.assets_sha256
    return metadata


def _metadata_keys(row: dict, rep: SourceRepresentation) -> set[str]:
    keys = set(REPRESENTATION_FIELDS)
    if rep.assets_sha256 or ASSET_HASH_FIELD in row:
        keys.add(ASSET_HASH_FIELD)
    return keys


def _bind_representation(row: dict, rep: SourceRepresentation, source: pathlib.Path) -> dict:
    expected = _representation_metadata(rep)
    present = REPRESENTATION_FIELDS & row.keys()
    if row.get(ASSET_HASH_FIELD) != rep.assets_sha256:
        raise RuntimeError(f"source assets_sha256 missing or mismatched for {source.name}")
    if not present:
        return {**row, **expected}
    if present != REPRESENTATION_FIELDS:
        missing = sorted(REPRESENTATION_FIELDS - present)
        raise RuntimeError(f"source representation metadata incomplete for {source.name}: {missing}")
    drift = sorted(key for key in _metadata_keys(row, rep) if row.get(key) != expected.get(key))
    if drift:
        raise RuntimeError(f"source representation drift for {source.name}: {drift}")
    return dict(row)


def _manifest_representation(source: pathlib.Path, adapters: tuple[SourceAdapter, ...]) -> SourceRepresentation:
    try:
        return representation_for(source, adapters)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"source representation invalid for {source.name}: {exc}") from exc


def bind_manifest(
    source_files: list[pathlib.Path], rows: list[dict], adapters: tuple[SourceAdapter, ...] = ADAPTERS
) -> list[dict]:
    """Verify original bytes and bind exact worker-representation provenance."""
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
        rep = _manifest_representation(source, adapters)
        if rep.original_sha256 != row.get("sha256"):
            raise RuntimeError(f"sha256 mismatch: {source.name}: {row.get('sha256')} != {rep.original_sha256}")
        bound.append(_bind_representation(row, rep, source))
    return bound
