"""Prepare and freeze source-mapped reading plans for research runs.

The public entry point consumes validated representations and writes one
byte-stable assignment before generation. Resume validates rather than replans.
"""

from __future__ import annotations

import json
import pathlib

from ._reading_inputs import _assets, _bibliography, _budget, _context, _default_readings, _source_records
from ._reading_plan import ReadingPlan, published_reading_plan
from ._reading_span import exact_excerpt, reading_quote, reading_source_text, structure
from ._reading_validation import policy
from .claims import atomic_write_json, stable_revision


def _structures(reps):
    sections, references, boundaries = {}, {}, {}
    for name, rep in reps.items():
        found, references[name], boundaries[name] = structure(name, rep)
        sections.update(found)
    return sections, references, boundaries


def _base(profile, sources, question, tokenizer):
    return {
        "version": 1,
        "question": question,
        "policy": policy(profile),
        "tokenizer": tokenizer,
        "works": profile["works"],
        "sources": sources,
    }


def _build_plan(root, profile, sources, bundles, reps, base, encoding):
    sections, references, boundaries = _structures(reps)
    reviewed = profile.get("reviewed_plan")
    chosen = json.loads((root / reviewed).read_text()) if reviewed else {}
    data = {**base, "sections": chosen.get("sections", sections)}
    data["readings"] = chosen.get("readings") or _default_readings(
        data["sections"], sources, reps, encoding, data["policy"]["target_tokens"]
    )
    for reading in data["readings"]:
        reading.setdefault("context", _context(reading, data, reps, references))
        reading["token_counts"] = _budget(reading, data["sections"], reps, encoding)
        reading["assets"] = _assets(reading, data["sections"], bundles)
    data["sha256"] = stable_revision(data)
    return data, boundaries


def _frozen_identity(data):
    keys = ("version", "question", "policy", "tokenizer", "works", "sources")
    return {key: data.get(key) for key in keys}


def prepare_reading_plan(source_root, project, manifest_rows, destination, bundle_directory, *, question=None):
    """Create once, then only accept a byte-stable plan bound to current sources and policy."""
    root, destination = pathlib.Path(source_root).resolve(), pathlib.Path(destination)
    profile = project["reading"]
    sources, bundles, reps = _source_records(root, profile, manifest_rows, bundle_directory)
    _bibliography(profile["works"], reps)
    from .sources import _reading_tokenizer

    encoding, tokenizer = _reading_tokenizer()
    base = _base(profile, sources, question, tokenizer)
    if destination.exists():
        data = json.loads(destination.read_text())
        if _frozen_identity(data) != base:
            raise ValueError("frozen reading plan question, source, bibliography, policy or tokenizer drift")
        _, _, boundaries = _structures(reps)
    else:
        data, boundaries = _build_plan(root, profile, sources, bundles, reps, base, encoding)
    plan = ReadingPlan(data, reps, boundaries)
    plan.validate()
    if not destination.exists():
        atomic_write_json(destination, data)
    return plan, bundles
