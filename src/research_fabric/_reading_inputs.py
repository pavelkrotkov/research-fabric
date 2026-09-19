"""Derive deterministic reading inputs from validated Markdown sources.

Source identity, bibliographic evidence, coherent chunks, context references,
asset closure, and token-budget observations are computed here.

The helpers consume existing adapter, manifest, and bundle owners rather than
reimplementing discovery or hashing. Token counts are frozen planner observations;
they never gate evidence acceptance.

Reviewed plans may replace deterministic boundaries, but never source identity.
"""

from __future__ import annotations

import re

from ._reading_span import span_text
from ._source_assets import safe_path
from .claims import stable_revision


def _authors(work):
    """Require authorship to be evidence-backed or explicitly unknown.

    An empty list is ambiguous: use ``None`` when the source does not establish authorship.
    """
    if "authors" not in work:
        raise ValueError("work bibliography must make unknown title/authors explicit")
    authors = work["authors"]
    if authors is None:
        return []
    if not isinstance(authors, list):
        raise ValueError("work authors must be a nonempty list or explicit null")
    if not authors:
        raise ValueError("work authors must be a nonempty list or explicit null")
    return authors


def _bibliographic_values(work):
    """Return only claims that must be supported by explicit bibliography spans.

    Filenames and project labels are deliberately excluded as attribution evidence.
    """
    if "title" not in work:
        raise ValueError("work bibliography must make unknown title/authors explicit")
    values = []
    if work["title"] is not None:
        values.append(work["title"])
    values.extend(_authors(work))
    return values


def _valid_bibliographic_text(values):
    for value in values:
        if not isinstance(value, str):
            return False
        if not value.strip():
            return False
    return True


def _work_values(work_id, work):
    if not isinstance(work_id, str):
        raise ValueError("reading works require explicit identities and bibliographic records")
    if not work_id:
        raise ValueError("reading works require explicit identities and bibliographic records")
    if not isinstance(work, dict):
        raise ValueError("reading works require explicit identities and bibliographic records")
    values = _bibliographic_values(work)
    if not _valid_bibliographic_text(values):
        raise ValueError("work bibliography must contain nonempty text or explicit null")
    return values


def _bibliography(works, representations):
    """Reject bibliographic metadata that is not quoted from declared source spans.

    This prevents convenient filenames or project metadata from becoming provenance.
    """
    for work_id, work in works.items():
        values = _work_values(work_id, work)
        evidence = "\n".join(span_text(representations, span) for span in work.get("bibliography_evidence", []))
        for value in values:
            if value not in evidence:
                raise ValueError(f"bibliographic attribution has no source evidence: {work_id}")


def _split_reading(primary, key, sections, representations, encoding, target):
    if not primary:
        return False
    section = sections[key]
    if section["source_file"] != sections[primary[0]]["source_file"]:
        return True
    combined = "".join(span_text(representations, sections[item]) for item in [*primary, key])
    return len(encoding.encode(combined, disallowed_special=())) > target


def _reading_chunks(sections, representations, encoding, target):
    """Group whole coherent sections; the token target never splits a source block.

    Oversized sections remain intact and are recorded above the soft budget for review.
    """
    primary = []
    for key, section in sections.items():
        if section["disposition"] != "primary":
            continue
        if _split_reading(primary, key, sections, representations, encoding, target):
            yield primary
            primary = []
        primary.append(key)
    if primary:
        yield primary


def _default_readings(sections, sources, representations, encoding, target):
    readings = []
    for primary in _reading_chunks(sections, representations, encoding, target):
        work_id = sources[sections[primary[0]]["source_file"]]["work_id"]
        row = {"primary": primary, "work_id": work_id}
        row["id"] = "read-" + stable_revision(row)[:20]
        readings.append(row)
    return readings


def _reference_section(line, source_file, sections):
    if line is None:
        return None
    for section_id, row in sections.items():
        if row["source_file"] == source_file and row["lines"][0] <= line + 1 < row["lines"][1]:
            return section_id
    return None


def _context(reading, data, representations, references):
    """Attach only declared or locally referenced definition sections as context.

    Context is carried with provenance but later projects to no independence group.
    """
    sections = data["sections"]
    context = list(data["works"][reading["work_id"]].get("context", []))
    for key in reading["primary"]:
        span = sections[key]
        labels = re.findall(r"\[([^\]]+)\]", span_text(representations, span))
        for label in labels:
            match = _reference_section(
                references[span["source_file"]].get(label.upper()), span["source_file"], sections
            )
            if match:
                context.append(match)
    return list(dict.fromkeys(key for key in context if key not in reading["primary"]))


def _assets(reading, sections, bundles):
    """Bind only visual assets whose source locators intersect assigned spans.

    Asset discovery itself remains owned by the existing source-bundle layer.
    """
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


def _representations(root, assignments):
    from .sources import representation_for

    """Load each explicitly configured logical path through the existing adapters.

    Planning never scans by basename, so nested works with repeated filenames stay distinct.
    """
    paths = [safe_path(root, name) for name in assignments]
    reps = {path: representation_for(path) for path in paths}
    if any(rep.adapter != "markdown" for rep in reps.values()):
        raise ValueError("source-mapped reading currently requires Markdown; legacy projects are unchanged")
    return paths, reps


def _bundle_record(root, profile, row, rep, bundle_directory):
    from .sources import prepare_source_bundle, source_attestation

    """Reuse the existing source-bundle identity instead of creating a parallel asset model.

    The reading plan stores only the bundle key and verified logical source identity.
    """
    work_id = profile["sources"][row["source_file"]]
    if work_id not in profile["works"] or not row.get("source_id"):
        raise ValueError("reading source needs a known work and manifest source ID")
    bundle = prepare_source_bundle(rep.path, bundle_directory, source_attestation(rep, root), source_root=root)
    return {**row, "work_id": work_id, "bundle_key": bundle["key"]}, bundle


def _source_records(root, profile, manifest_rows, bundle_directory):
    from .sources import bind_manifest

    """Bind explicit logical source paths to existing manifest and bundle identities.

    Nested duplicate basenames stay distinct because lookup is relative to ``root``.
    """
    assignments = profile.get("sources")
    if not isinstance(assignments, dict) or not assignments:
        raise ValueError("reading profile requires explicit source-file to work assignments")
    paths, by_path = _representations(root, assignments)
    rows = bind_manifest(paths, manifest_rows, source_root=root, representations=by_path)
    reps = {path.relative_to(root).as_posix(): rep for path, rep in by_path.items()}
    sources, bundles = {}, {}
    for row in rows:
        name = row["source_file"]
        sources[name], bundles[name] = _bundle_record(root, profile, row, reps[name], bundle_directory)
    if len({row["source_id"] for row in sources.values()}) != len(sources):
        raise ValueError("reading sources have ambiguous manifest identities")
    return sources, bundles, reps


def _budget(reading, sections, representations, encoding):
    """Record source and context size observations without turning them into gates.

    The configured budgets are planning hypotheses; coherent evidence may exceed them.
    """
    return {
        role: sum(
            len(encoding.encode(span_text(representations, sections[key]), disallowed_special=()))
            for key in reading[role]
        )
        for role in ("primary", "context")
    }

