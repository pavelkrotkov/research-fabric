"""Format-independent source loading and provenance."""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass
from itertools import accumulate

from ._source_adapter import ADAPTERS, SourceAdapter, _lines
from .reading import (
    ReadingPlan as ReadingPlan,
    exact_excerpt as exact_excerpt,
    prepare_reading_plan as prepare_reading_plan,
    published_reading_plan as published_reading_plan,
    reading_quote as reading_quote,
    reading_source_text as reading_source_text,
)

REPRESENTATION_FIELDS = frozenset({"adapter", "adapter_version", "representation_encoding", "representation_sha256"})
ASSET_HASH_FIELD = "assets_sha256"
SOURCE_METADATA_FIELD = "source_metadata"


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
    source_metadata: tuple[tuple[str, str], ...] = ()
    original_line_offsets: tuple[int, ...] = ()

    @property
    def grounding_text(self) -> str:
        """Remove generated EPUB markers before entity folding, retaining visible literals."""
        if self.adapter == "epub":
            return re.sub(r"<!--@@(?:chapter |section |asset |formula)[^>]*-->", " ", self.text)
        return self.text

    def original_byte_offset(self, position: int) -> int:
        """Map a Markdown character boundary back to its immutable UTF-8 snapshot."""
        if self.adapter != "markdown" or not 0 <= position <= len(self.text):
            raise ValueError("source position has no exact original-byte mapping")
        line = self.text.count("\n", 0, position)
        column_start = self.text.rfind("\n", 0, position) + 1
        return self.original_line_offsets[line] + len(self.text[column_start:position].encode("utf-8"))


def source_attestation(rep: SourceRepresentation, source_root: pathlib.Path | None = None) -> dict:
    """Return the exact source input attestation consumed by a worker."""
    return {
        "source_file": rep.path.resolve().relative_to(source_root.resolve()).as_posix()
        if source_root is not None
        else rep.path.name,
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
    from ._source_assets import safe_path

    return safe_path(source.parent, relative)


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
    metadata = adapter.metadata(path)
    source_metadata = tuple(
        sorted((key, value) for key, value in metadata.items() if key not in {"content_type", "filename"})
    )
    return SourceRepresentation(
        path=path,
        adapter=adapter.name,
        adapter_version=adapter.version,
        text=text,
        original_sha256=hashlib.sha256(raw).hexdigest(),
        representation_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        assets=assets,
        assets_sha256=_assets_sha256(path, assets),
        source_metadata=source_metadata,
        original_line_offsets=(
            tuple(accumulate((len(line.encode("utf-8")) for line in _lines(decoded)), initial=0))
            if adapter.name == "markdown"
            else ()
        ),
    )


def source_bundle(path: pathlib.Path) -> tuple[tuple[pathlib.Path, pathlib.Path], ...]:
    """Return source + local referenced assets as (input, relative-output) pairs."""
    rep = representation_for(path)
    assets = tuple((_asset_path(path, asset), pathlib.Path(asset)) for asset in rep.assets)
    return ((path, pathlib.Path(path.name)), *assets)


def source_provenance(
    source_files: list[pathlib.Path],
    adapters: tuple[SourceAdapter, ...] = ADAPTERS,
    *,
    source_root: pathlib.Path | None = None,
) -> list[dict[str, str]]:
    """Hash and describe the representations supplied to one worker."""
    return sorted(
        (source_attestation(representation_for(path, adapters), source_root) for path in source_files),
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


def _representation_metadata(rep: SourceRepresentation) -> dict[str, object]:
    metadata: dict[str, object] = {
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
    }
    if rep.assets_sha256:
        metadata[ASSET_HASH_FIELD] = rep.assets_sha256
    if rep.source_metadata:
        metadata[SOURCE_METADATA_FIELD] = dict(rep.source_metadata)
    return metadata


def _metadata_keys(row: dict, rep: SourceRepresentation) -> set[str]:
    keys = set(REPRESENTATION_FIELDS)
    if rep.assets_sha256 or ASSET_HASH_FIELD in row:
        keys.add(ASSET_HASH_FIELD)
    if rep.source_metadata or SOURCE_METADATA_FIELD in row:
        keys.add(SOURCE_METADATA_FIELD)
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


def _manifest_rows(rows, source_root):
    by_name = {}
    for row in rows:
        name = row.get("source_file") if source_root is not None else pathlib.Path(row.get("snapshot", "")).name
        if not name:
            raise RuntimeError("source manifest row missing snapshot")
        if name in by_name:
            raise RuntimeError(f"duplicate source manifest entry: {name}")
        by_name[name] = row
    return by_name


def _bound_manifest_row(source, rows, adapters, source_root, representations):
    name = source.relative_to(source_root).as_posix() if source_root is not None else source.name
    row = rows.get(name)
    if row is None:
        raise RuntimeError(f"source manifest has no entry for {source.name}")
    rep = representations[source] if representations is not None else _manifest_representation(source, adapters)
    if rep.original_sha256 != row.get("sha256"):
        raise RuntimeError(f"sha256 mismatch: {source.name}: {row.get('sha256')} != {rep.original_sha256}")
    return _bind_representation(row, rep, source)


def bind_manifest(
    source_files: list[pathlib.Path],
    rows: list[dict],
    adapters: tuple[SourceAdapter, ...] = ADAPTERS,
    *,
    source_root: pathlib.Path | None = None,
    representations: dict | None = None,
) -> list[dict]:
    """Verify original bytes and bind exact worker-representation provenance."""
    by_name = _manifest_rows(rows, source_root)
    return [_bound_manifest_row(source, by_name, adapters, source_root, representations) for source in source_files]


def prepare_source_bundle(
    source: pathlib.Path, destination: pathlib.Path, attestation: dict, *, source_root: pathlib.Path | None = None
) -> dict:
    """Extend the existing source attestation with a frozen visual asset closure."""
    from ._source_assets import prepare_bundle

    if source_attestation(representation_for(source), source_root) != attestation:
        raise ValueError(f"source bundle attestation drift: {source.name}")
    return prepare_bundle(source, destination, attestation)


def snapshot_relative(source: pathlib.Path, source_root: pathlib.Path | None = None) -> pathlib.Path:
    from ._source_assets import bundle_key

    attestation = source_attestation(representation_for(source), source_root)
    return pathlib.Path(bundle_key(attestation)) / source.name


def copy_source_snapshot(
    source: pathlib.Path, snapshots: pathlib.Path, *, source_root: pathlib.Path | None = None
) -> pathlib.Path:
    """The original document and local assets share one collision-free namespace."""
    import shutil

    from ._source_assets import safe_path

    relative = snapshot_relative(source, source_root)
    for original, asset_relative in source_bundle(source):
        destination = safe_path(snapshots, (relative.parent / asset_relative).as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != original.read_bytes():
                raise ValueError(f"source snapshot drift: {destination}")
            destination.chmod(0o444)
            continue
        shutil.copy2(original, destination)
        destination.chmod(0o444)
    return snapshots / relative


def _reading_tokenizer():
    """Use the qualified BPE data, explicitly provisioned before an offline run."""
    import importlib.metadata
    import os
    import platform

    import tiktoken

    cache = os.environ.get("TIKTOKEN_CACHE_DIR")
    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    expected = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    path = pathlib.Path(cache or "") / hashlib.sha1(url.encode()).hexdigest()
    if not cache or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("provision verified cl100k_base data in TIKTOKEN_CACHE_DIR before reading-plan preparation")
    encoding = tiktoken.get_encoding("cl100k_base")
    record = {
        "encoding": encoding.name,
        "package": importlib.metadata.version("tiktoken"),
        "python": platform.python_version(),
        "data_sha256": expected,
        "count_semantics": "cl100k_base BPE; source/context counted separately; not provider usage",
    }
    return encoding, record


def preserve_original_bytes(field_root):
    """Original snapshots and native raw input copies are data, with readable Git diffs."""
    from .claims import atomic_write_text

    path = field_root / ".gitattributes"
    original = path.read_bytes().decode("utf-8") if path.exists() else ""
    current = original.replace("\r\n", "\n").replace("\r", "\n")
    rules = (
        "evidence/snapshots/** -text -whitespace",
        "wiki/assets/**/original/** -text -whitespace",
        "raw/** -text -whitespace",
        "wiki/sources/** -text -whitespace",
    )
    block = "\n".join(rules) + "\n"
    if not current.endswith(block):
        separator = "" if not current or current.endswith("\n") else "\n"
        current += separator + block
    if current != original:
        atomic_write_text(path, current)

