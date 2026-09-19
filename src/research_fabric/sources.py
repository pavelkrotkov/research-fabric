"""Format-independent source loading and provenance."""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass
from itertools import accumulate

from ._source_adapter import ADAPTERS, SourceAdapter, _lines

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


def bind_manifest(
    source_files: list[pathlib.Path],
    rows: list[dict],
    adapters: tuple[SourceAdapter, ...] = ADAPTERS,
    *,
    source_root: pathlib.Path | None = None,
    representations: dict | None = None,
) -> list[dict]:
    """Verify original bytes and bind exact worker-representation provenance."""
    by_name = {}
    for row in rows:
        name = row.get("source_file") if source_root is not None else pathlib.Path(row.get("snapshot", "")).name
        if not name:
            raise RuntimeError("source manifest row missing snapshot")
        if name in by_name:
            raise RuntimeError(f"duplicate source manifest entry: {name}")
        by_name[name] = row
    bound = []
    for source in source_files:
        name = source.relative_to(source_root).as_posix() if source_root is not None else source.name
        row = by_name.get(name)
        if row is None:
            raise RuntimeError(f"source manifest has no entry for {source.name}")
        rep = representations[source] if representations is not None else _manifest_representation(source, adapters)
        if rep.original_sha256 != row.get("sha256"):
            raise RuntimeError(f"sha256 mismatch: {source.name}: {row.get('sha256')} != {rep.original_sha256}")
        bound.append(_bind_representation(row, rep, source))
    return bound


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


def _span_range(rep, span):
    bounds = span.get("lines")
    lines = _lines(rep.text)
    if not (
        isinstance(bounds, list)
        and len(bounds) == 2
        and all(type(n) is int for n in bounds)
        and 1 <= bounds[0] < bounds[1] <= len(lines) + 1
    ):
        raise ValueError("invalid reading source line range")
    return sum(map(len, lines[: bounds[0] - 1])), sum(map(len, lines[: bounds[1] - 1]))


def exact_excerpt(text, excerpt):
    """Return one exact occurrence; overlapping occurrences are also ambiguous."""
    if not isinstance(excerpt, str) or not excerpt:
        raise ValueError("reading claim has no exact excerpt")
    start = text.find(excerpt)
    if start < 0 or text.find(excerpt, start + 1) >= 0:
        raise ValueError("reading quote is missing, ambiguous, or outside its assigned source spans")
    return start


def reading_source_text(rep, claim):
    if "reading" not in claim:
        return rep.grounding_text
    start, end = _span_range(rep, claim["reading"])
    return rep.text[start:end]


def reading_quote(rep, claim):
    """Project current excerpt coordinates; repair never persists stale quote offsets."""
    start, end = _span_range(rep, claim["reading"])
    start += exact_excerpt(rep.text[start:end], claim.get("excerpt"))
    end = start + len(claim["excerpt"])
    return {
        "representation_bytes": [len(rep.text[:start].encode()), len(rep.text[:end].encode())],
        "original_bytes": [rep.original_byte_offset(start), rep.original_byte_offset(end)],
    }


def _span_text(representations, span):
    rep = representations[span["source_file"]]
    start, end = _span_range(rep, span)
    return rep.text[start:end]


def _protected_boundaries(lines, tokens, references):
    blocked = set()
    inert = set()
    for token in tokens:
        if token.level == 0 and token.map:
            blocked.update(range(token.map[0] + 1, token.map[1]))
        if token.type in {"fence", "code_block"} and token.map:
            inert.update(range(*token.map))
    for reference in references.values():
        blocked.update(range(reference["map"][0] + 1, reference["map"][1]))
    opening = None
    for index, line in enumerate(lines):
        if index not in inert and line.strip() == "$$":
            if opening is None:
                opening = index
            else:
                blocked.update(range(opening + 1, index + 1))
                opening = None
    if opening is not None:
        blocked.update(range(opening + 1, len(lines)))
    if lines[0].strip() == "---":
        closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() in {"---", "..."}), 0)
        blocked.update(range(1, closing + 1))
    return blocked


def _reading_structure(name, rep):
    from ._source_adapter import _MARKDOWN

    lines = _lines(rep.text)
    if not lines:
        raise ValueError(f"empty reading source: {name}")
    environment = {}
    tokens = _MARKDOWN.parse(rep.text, environment)
    allowed = set(range(len(lines) + 1)) - _protected_boundaries(lines, tokens, environment.get("references", {}))
    starts = sorted(
        {
            0,
            len(lines),
            *(t.map[0] for t in tokens if t.type == "heading_open" and t.level == 0 and t.map[0] in allowed),
        }
    )
    sections = {
        f"{name}#L{a + 1}-{b}": {"source_file": name, "lines": [a + 1, b + 1], "disposition": "primary"}
        for a, b in zip(starts, starts[1:])
    }
    definitions = {key: row["map"][0] for key, row in environment.get("references", {}).items()}
    for token in tokens:
        match = re.match(r"\[\^([^\]]+)\]:", token.content) if token.type == "inline" else None
        if match:
            definitions["^" + match[1].upper()] = token.map[0]
    return sections, definitions, allowed


def _bibliographic_values(work):
    title, authors = work["title"], work["authors"]
    if authors is not None and (not isinstance(authors, list) or not authors):
        raise ValueError("work authors must be a nonempty list or explicit null")
    values = ([title] if title is not None else []) + (authors or [])
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("work bibliography must contain nonempty text or explicit null")
    return values


def _bibliography(works, representations):
    for work_id, work in works.items():
        if not isinstance(work_id, str) or not work_id or not isinstance(work, dict):
            raise ValueError("reading works require explicit identities and bibliographic records")
        values = _bibliographic_values(work)
        evidence = "\n".join(_span_text(representations, span) for span in work.get("bibliography_evidence", []))
        if any(value not in evidence for value in values):
            raise ValueError(f"bibliographic attribution has no source evidence: {work_id}")


def _default_readings(sections, sources, representations, encoding, target):
    from .claims import stable_revision

    readings, primary = [], []
    for key, section in sections.items():
        if section["disposition"] != "primary":
            continue
        combined = "".join(_span_text(representations, sections[item]) for item in [*primary, key])
        different_source = primary and section["source_file"] != sections[primary[0]]["source_file"]
        if primary and (different_source or len(encoding.encode(combined, disallowed_special=())) > target):
            readings.append({"primary": primary})
            primary = []
        primary.append(key)
    if primary:
        readings.append({"primary": primary})
    for reading in readings:
        reading["work_id"] = sources[sections[reading["primary"][0]]["source_file"]]["work_id"]
        reading["id"] = "read-" + stable_revision(reading)[:20]
    return readings


def _reading_context(reading, data, representations, references):
    sections = data["sections"]
    context = list(data["works"][reading["work_id"]].get("context", []))
    for key in reading["primary"]:
        span = sections[key]
        text = _span_text(representations, span)
        for label in re.findall(r"\[([^\]]+)\]", text):
            line = references[span["source_file"]].get(label.upper())
            if line is not None:
                context.append(
                    next(
                        key
                        for key, section in sections.items()
                        if section["source_file"] == span["source_file"]
                        and section["lines"][0] <= line + 1 < section["lines"][1]
                    )
                )

    return list(dict.fromkeys(key for key in context if key not in reading["primary"]))


def _reading_budget(reading, sections, representations, encoding):
    counts = {
        role: sum(
            len(encoding.encode(_span_text(representations, sections[key]), disallowed_special=()))
            for key in reading[role]
        )
        for role in ("primary", "context")
    }
    return {"token_counts": counts}


def _reading_assets(reading, sections, bundles):
    spans = [sections[key] for key in [*reading["primary"], *reading["context"]]]
    return [
        {"source_file": name, "index": index}
        for name, bundle in bundles.items()
        for index, asset in enumerate(bundle["assets"])
        if any(
            span["source_file"] == name
            and max(span["lines"][0], asset["locator"]["lines"][0])
            < min(span["lines"][1], asset["locator"]["lines"][1])
            for span in spans
        )
    ]


def _validate_sections(sections, representations, boundaries):
    for name, rep in representations.items():
        ranges = sorted(section["lines"] for section in sections.values() if section["source_file"] == name)
        if not ranges or ranges[0][0] != 1 or ranges[-1][1] != len(_lines(rep.text)) + 1:
            raise ValueError("reading section coverage has gaps")
        if any(left[1] != right[0] for left, right in zip(ranges, ranges[1:])):
            raise ValueError("reading section coverage has gaps or overlaps")
    for section in sections.values():
        _span_range(representations[section["source_file"]], section)
        if not {n - 1 for n in section["lines"]}.issubset(boundaries[section["source_file"]]):
            raise ValueError("reviewed reading boundary splits a coherent source block")
        if section["disposition"] not in {"primary", "context", "excluded"}:
            raise ValueError("unknown reading section disposition")
        if section["disposition"] != "primary" and not (
            isinstance(section.get("reason"), str) and section["reason"].strip()
        ):
            raise ValueError("non-primary reading section needs an explicit reason")


def _validate_reading(reading, sections, sources):
    if not reading["primary"]:
        raise ValueError("reading assignment has no primary source")
    ranges = {}
    for role in ("primary", "context"):
        for key in reading[role]:
            span = sections[key]
            if role == "primary" and sources[span["source_file"]]["work_id"] != reading["work_id"]:
                raise ValueError("reading primary assignment crosses parent works")
            ranges.setdefault(span["source_file"], []).append(span["lines"])
    for spans in ranges.values():
        ordered = sorted(spans)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("reading primary/context source spans overlap")


def _validate_ownership(readings, sections):
    from collections import Counter

    ids = [row["id"] for row in readings]
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) for value in ids):
        raise ValueError(
            "reading ID must use 1-128 ASCII letters, digits, hyphens or underscores; start with a letter or digit"
        )
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("reading plan has missing or duplicate assignments")
    primary = Counter(key for reading in readings for key in reading["primary"])
    expected = Counter({key: 1 for key, section in sections.items() if section["disposition"] == "primary"})
    if primary != expected:
        raise ValueError("reading section has missing or overlapping primary ownership")
    context = {key for reading in readings for key in reading["context"]}
    required = {key for key, section in sections.items() if section["disposition"] == "context"}
    excluded = {key for key, section in sections.items() if section["disposition"] == "excluded"}
    if not required.issubset(context) or context & excluded:
        raise ValueError("reading context does not match section dispositions")


class ReadingPlan:
    """One frozen source assignment shared by dispatch, publication and resume."""

    def __init__(self, data, representations, boundaries):
        self.data, self.representations, self.boundaries = data, representations, boundaries

    @classmethod
    def load(cls, path, source_paths, profile=None):
        """Load frozen assignments against exact current document paths, including published snapshots."""
        import json

        data = json.loads(pathlib.Path(path).read_text())
        if profile is not None and (
            data["policy"] != _reading_policy(profile)
            or data["works"] != profile["works"]
            or {name: row["work_id"] for name, row in data["sources"].items()} != profile["sources"]
        ):
            raise ValueError("frozen reading plan project policy or work assignment drift")
        if set(source_paths) != set(data["sources"]):
            raise ValueError("frozen reading plan source set drift")
        representations, boundaries = {}, {}
        for name, source in source_paths.items():
            rep = representation_for(pathlib.Path(source))
            expected = {**source_attestation(rep), "source_file": name}
            if any(data["sources"][name].get(key) != value for key, value in expected.items()):
                raise ValueError(f"frozen reading plan source drift: {name}")
            _bind_representation(data["sources"][name], rep, rep.path)
            representations[name] = rep
            _, _, boundaries[name] = _reading_structure(name, rep)
        plan = cls(data, representations, boundaries)
        plan.validate()
        return plan

    def section(self, section_id):
        """Resolve a section citation against the same span map used for quote projection."""
        span = self.data["sections"][section_id]
        rep = self.representations[span["source_file"]]
        start, end = _span_range(rep, span)
        from urllib.parse import quote

        key = self.data["sources"][span["source_file"]]["bundle_key"]
        return {
            **span,
            "target": f"assets/{key}/original/{quote(span['source_file'])}#L{span['lines'][0]}-L{span['lines'][1] - 1}",
            "original_bytes": [rep.original_byte_offset(start), rep.original_byte_offset(end)],
        }

    def citation_policy(self):
        """Compact original-section mapping for native OpenKB's supported per-KB policy."""
        import json

        records = []
        for section_id in self.data["sections"]:
            section = self.section(section_id)
            heading = _span_text(self.representations, section).splitlines()[0]
            records.append(json.dumps({"section": section_id, "heading": heading, **section}, ensure_ascii=False))
        return (
            "\n## Frozen research source citations\n"
            f"Reading plan SHA256: {self.data['sha256']}\n"
            "Cite original source sections using the targets below (paths relative to wiki root; "
            "from summaries/concepts use ../assets/...). Prepared document line numbers are not original lines. "
            "Retain mathematical qualifications, definitions, uncertainty and conflicting results. "
            "Context is not independent primary evidence. Bibliographic unknowns remain unknown. "
            "These mappings identify original ranges; they do not prove semantic support.\n" + "\n".join(records) + "\n"
        )

    def validate_claim(self, claim):
        """Check assignment identity without requiring a rejected excerpt to ground before repair."""
        if "source_ids" in claim and claim["source_ids"] != [self.data["sources"][claim["source_file"]]["source_id"]]:
            raise ValueError("reading claim source IDs differ from frozen source identity")
        binding = claim["reading"]
        if not any(
            binding == expected for expected, _ in self._assignments(binding["reading_id"], claim["source_file"])
        ):
            raise ValueError("accepted reading assignment differs from frozen plan")

    def reading(self, reading_id):
        matches = [row for row in self.data["readings"] if row["id"] == reading_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous reading assignment: {reading_id}")
        return matches[0]

    def source_names(self, reading_id):
        reading = self.reading(reading_id)
        return sorted(
            {self.data["sections"][key]["source_file"] for role in ("primary", "context") for key in reading[role]}
        )

    def input(self, reading_id):
        """Generated labels have no source range and can never ground an excerpt."""
        parts = []
        for role in ("primary", "context"):
            for key in self.reading(reading_id)[role]:
                span = self.data["sections"][key]
                start, end = span["lines"]
                parts.extend(
                    [
                        f"\n[{role.upper()} SOURCE {span['source_file']} L{start}-{end - 1}]\n",
                        _span_text(self.representations, span),
                    ]
                )
        return "".join(parts)

    def _assignments(self, reading_id, source_file):
        """One authority for both quote acceptance and immutable assignment validation."""
        for role in ("primary", "context"):
            for key in self.reading(reading_id)[role]:
                span = self.data["sections"][key]
                if span["source_file"] == source_file:
                    yield (
                        {
                            "plan_sha256": self.data["sha256"],
                            "reading_id": reading_id,
                            "work_id": self.data["sources"][source_file]["work_id"],
                            "role": role,
                            "lines": span["lines"],
                        },
                        span,
                    )

    def claim(self, reading_id, claim):
        """Resolve the quote once; context retains its cited work, not the reader's work."""
        matches = [
            binding
            for binding, span in self._assignments(reading_id, claim.get("source_file"))
            if claim.get("excerpt") in _span_text(self.representations, span)
        ]
        if len(matches) != 1:
            raise ValueError("reading quote is missing, ambiguous, or outside its assigned source spans")
        binding = matches[0]
        return binding, reading_quote(self.representations[claim["source_file"]], {**claim, "reading": binding})

    def project_claim(self, claim, *, quotes=True):
        self.validate_claim(claim)
        projection = {
            "reading": claim["reading"],
            "independence_group": claim["reading"]["work_id"] if claim["reading"]["role"] == "primary" else None,
        }
        if quotes:
            projection["quote_span"] = reading_quote(self.representations[claim["source_file"]], claim)
        return projection

    @staticmethod
    def published_claim_errors(field_root, source_rows, claims, *, quotes=True):
        plan = published_reading_plan(field_root, source_rows)
        for claim in claims:
            if plan is None and "reading" not in claim:
                continue
            try:
                if plan is None:
                    raise ValueError("reading claim has no verified frozen plan")
                projection = plan.project_claim(claim, quotes=quotes)
                if any(claim.get(key) != value for key, value in projection.items()):
                    raise ValueError("reading claim projection differs from frozen assignment/current excerpt")
            except (ValueError, KeyError, TypeError) as exc:
                yield claim.get("claim_id"), str(exc)

    def validate_packet(self, packet, reading_id, acceptance):
        """Use existing claim shape checks, then bind every quote to the frozen assignment."""
        from .core import packet_defects

        if packet.get("worker") != reading_id:
            raise ValueError("reading packet worker differs from dispatched assignment")
        defects = packet_defects(packet.get("parsed"))
        if defects:
            raise ValueError("; ".join(defects))
        primary = 0
        for claim in packet["parsed"]["claims"]:
            binding, _ = self.claim(reading_id, claim)
            if claim.get("reading") != binding:
                raise ValueError("accepted reading assignment differs from frozen plan")
            primary += binding["role"] == "primary"
        if primary < acceptance.get("min_claims_per_reading", 1):
            raise ValueError("reading packet has too few primary evidence claims")
        limit = acceptance.get("max_claims_per_reading")
        if limit is not None and len(packet["parsed"]["claims"]) > limit:
            raise ValueError("reading packet exceeds maximum evidence claims")

    def validate(self):
        from .claims import stable_revision

        data = self.data
        if data.get("version") != 1:
            raise ValueError("unsupported reading plan version")
        if data.get("sha256") != stable_revision({key: value for key, value in data.items() if key != "sha256"}):
            raise ValueError("reading plan digest drift")
        _validate_sections(data["sections"], self.representations, self.boundaries)
        _validate_ownership(data["readings"], data["sections"])
        for reading in data["readings"]:
            _validate_reading(reading, data["sections"], data["sources"])


def _reading_policy(profile):
    policy = {
        "version": 1,
        "target_tokens": 6000,
        "soft_limit_tokens": 12000,
        "context_budget_tokens": 2000,
        **profile.get("policy", {}),
    }
    if (
        set(policy) != {"version", "target_tokens", "soft_limit_tokens", "context_budget_tokens"}
        or policy["version"] != 1
    ):
        raise ValueError("unsupported reading policy")
    if any(type(value) is not int or value < 1 for value in policy.values()):
        raise ValueError("reading budgets must be positive integers")
    return policy


def _reading_sources(source_root, profile, manifest_rows, bundle_directory):
    from ._source_assets import safe_path

    assignments = profile["sources"]
    if not isinstance(assignments, dict) or not assignments:
        raise ValueError("reading profile requires explicit source-file to work assignments")
    paths = [safe_path(source_root, name) for name in assignments]
    representations = {path: representation_for(path) for path in paths}
    if any(rep.adapter != "markdown" for rep in representations.values()):
        raise ValueError(
            "source-mapped reading currently requires Markdown; legacy HTML/witness projects are unchanged"
        )
    bound = bind_manifest(paths, manifest_rows, source_root=source_root, representations=representations)
    reps = {path.relative_to(source_root).as_posix(): rep for path, rep in representations.items()}
    sources, bundles = {}, {}
    for row in bound:
        name = row["source_file"]
        if assignments[name] not in profile["works"] or not row.get("source_id"):
            raise ValueError("reading source needs a known work and manifest source ID")
        rep = reps[name]
        bundle = prepare_source_bundle(
            rep.path, bundle_directory, source_attestation(rep, source_root), source_root=source_root
        )
        bundles[name] = bundle
        sources[name] = {**row, "work_id": assignments[name], "bundle_key": bundle["key"]}
    ids = [row["source_id"] for row in sources.values()]
    if len(ids) != len(set(ids)):
        raise ValueError("reading sources have ambiguous manifest identities")
    return sources, bundles, reps


def prepare_reading_plan(source_root, project, manifest_rows, destination, bundle_directory, *, question=None):
    """Verify sources, then create or validate the frozen assignment before generation."""
    import json

    from .claims import atomic_write_json, stable_revision

    source_root = pathlib.Path(source_root).resolve()
    profile = project["reading"]
    sources, bundles, reps = _reading_sources(source_root, profile, manifest_rows, bundle_directory)
    _bibliography(profile["works"], reps)
    encoding, tokenizer = _reading_tokenizer()
    policy = _reading_policy(profile)
    sections, references, boundaries = {}, {}, {}
    for name, rep in reps.items():
        source_sections, references[name], boundaries[name] = _reading_structure(name, rep)
        sections.update(source_sections)
    base = {
        "version": 1,
        "question": question,
        "policy": policy,
        "tokenizer": tokenizer,
        "works": profile["works"],
        "sources": sources,
    }
    if destination.exists():
        data = json.loads(destination.read_text())
        if any(data.get(key) != value for key, value in base.items()):
            raise ValueError("frozen reading plan question, source, bibliography, policy or tokenizer drift")
    else:
        reviewed = profile.get("reviewed_plan")
        chosen = json.loads((source_root / reviewed).read_text()) if reviewed else {}
        sections = chosen.get("sections", sections)
        readings = chosen.get("readings") or _default_readings(
            sections, sources, reps, encoding, policy["target_tokens"]
        )
        data = {**base, "sections": sections, "readings": readings}
        for reading in readings:
            reading.setdefault("context", _reading_context(reading, data, reps, references))
            reading.update(_reading_budget(reading, sections, reps, encoding))
            reading["assets"] = _reading_assets(reading, sections, bundles)
        data["sha256"] = stable_revision(data)
    plan = ReadingPlan(data, reps, boundaries)
    plan.validate()
    if not destination.exists():
        atomic_write_json(destination, data)
    return plan, bundles


def published_reading_plan(field_root, source_rows):
    """Resolve logical source names through the published ledger, never physical basenames."""
    from ._source_assets import safe_path

    path = field_root / "evidence/reading-plan.json"
    if not path.exists():
        return None
    paths = {row["source_file"]: safe_path(field_root, row["snapshot"]) for row in source_rows}
    if len(paths) != len(source_rows):
        raise ValueError("ambiguous published reading source identity")
    return ReadingPlan.load(path, paths)


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
