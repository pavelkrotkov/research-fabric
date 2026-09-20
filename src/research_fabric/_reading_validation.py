"""Validate frozen reading-plan structure without reparsing source assets.

Validation is fail-closed: coverage, disposition, ownership, worker identity,
and plan digest must all match the frozen assignment.
"""

from __future__ import annotations

import re

from ._reading_span import span_range
from ._source_adapter import _lines
from .claims import stable_revision


def policy(profile):
    """Normalize the small versioned planning policy stored in the frozen plan.

    Budgets are positive planning observations, never evidence-acceptance thresholds.
    """
    value = {
        "version": 1,
        "target_tokens": 6000,
        "soft_limit_tokens": 12000,
        "context_budget_tokens": 2000,
        **profile.get("policy", {}),
    }
    keys = {"version", "target_tokens", "soft_limit_tokens", "context_budget_tokens"}
    if set(value) != keys or value["version"] != 1:
        raise ValueError("unsupported reading policy")
    if any(type(v) is not int or v < 1 for v in value.values()):
        raise ValueError("reading budgets must be positive integers")
    return value


def _source_ranges(name, sections):
    return sorted(row["lines"] for row in sections.values() if row["source_file"] == name)


def _source_coverage(name, rep, sections):
    """Require complete, contiguous disposition of every source line.

    A reviewed plan may relabel sections but may not leave gaps or overlaps.
    """
    ranges = _source_ranges(name, sections)
    if not ranges:
        raise ValueError("reading section coverage has gaps")
    if ranges[0][0] != 1:
        raise ValueError("reading section coverage has gaps")
    if ranges[-1][1] != len(_lines(rep.text)) + 1:
        raise ValueError("reading section coverage has gaps")
    for left, right in zip(ranges, ranges[1:]):
        if left[1] != right[0]:
            raise ValueError("reading section coverage has gaps or overlaps")


def _section(row, representations, boundaries):
    """Reject reviewed boundaries that cut through a coherent Markdown block.

    Non-primary content also needs an explicit reason so omissions remain reviewable.
    """
    span_range(representations[row["source_file"]], row)
    if not {n - 1 for n in row["lines"]}.issubset(boundaries[row["source_file"]]):
        raise ValueError("reviewed reading boundary splits a coherent source block")
    if row["disposition"] not in {"primary", "context", "excluded"}:
        raise ValueError("unknown reading section disposition")
    if row["disposition"] != "primary" and not str(row.get("reason", "")).strip():
        raise ValueError("non-primary reading section needs an explicit reason")


def _reading_ids(readings):
    ids = [row["id"] for row in readings]
    if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", v) for v in ids):
        raise ValueError("reading ID must be a safe 1-128 character worker identity")
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("reading plan has missing or duplicate assignments")


def _reading_spans(reading, sections, sources):
    """Validate one assignment without allowing primary evidence to cross works.

    Context can cross works, but it is separately projected as non-independent evidence.
    """
    ranges = {}
    if not reading["primary"]:
        raise ValueError("reading assignment has no primary source")
    for role in ("primary", "context"):
        for key in reading[role]:
            span = sections[key]
            if role == "primary" and sources[span["source_file"]]["work_id"] != reading["work_id"]:
                raise ValueError("reading primary assignment crosses parent works")
            ranges.setdefault(span["source_file"], []).append(span["lines"])
    return ranges


def _no_overlap(ranges):
    for spans in ranges.values():
        ordered = sorted(spans)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("reading primary/context source spans overlap")


def _primary_ownership(readings, sections):
    primary = [key for reading in readings for key in reading["primary"]]
    expected = [key for key, row in sections.items() if row["disposition"] == "primary"]
    if sorted(primary) != sorted(expected):
        raise ValueError("reading section has missing or overlapping primary ownership")
    if len(primary) != len(set(primary)):
        raise ValueError("reading section has missing or overlapping primary ownership")


def _with_disposition(sections, disposition):
    return {key for key, row in sections.items() if row["disposition"] == disposition}


def _context_ownership(readings, sections):
    context = {key for reading in readings for key in reading["context"]}
    if not _with_disposition(sections, "context").issubset(context):
        raise ValueError("reading context does not match section dispositions")
    if context & _with_disposition(sections, "excluded"):
        raise ValueError("reading context does not match section dispositions")


def _plan_identity(data):
    if data.get("version") != 1:
        raise ValueError("unsupported reading plan version")
    payload = {key: value for key, value in data.items() if key != "sha256"}
    if data.get("sha256") != stable_revision(payload):
        raise ValueError("reading plan digest drift")


def validate_plan(data, representations, boundaries):
    """Check all frozen structural invariants before dispatch, resume, or publication.

    The same routine validates generated and loaded plans, preventing a rehashed plan from
    bypassing section coverage, coherent boundaries, ownership, or safe worker identities.
    """
    _plan_identity(data)
    for name, rep in representations.items():
        _source_coverage(name, rep, data["sections"])
    for row in data["sections"].values():
        _section(row, representations, boundaries)
    _reading_ids(data["readings"])
    _primary_ownership(data["readings"], data["sections"])
    _context_ownership(data["readings"], data["sections"])
    for reading in data["readings"]:
        _no_overlap(_reading_spans(reading, data["sections"], data["sources"]))
