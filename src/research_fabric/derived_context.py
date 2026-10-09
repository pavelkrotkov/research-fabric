"""Bounded, unreviewed visual context, separate from frozen source evidence."""

import json
from pathlib import Path, PurePosixPath

from ._source_assets import safe_path
from .claims import atomic_write_json, stable_revision

INSTRUCTION = (
    "DERIVED VISUAL CONTEXT — advisory, unreviewed, NOT verbatim source evidence. "
    "Inspection proves access, not correctness. Use these notes to guide questions and qualify interpretation. "
    "Do not quote generated transcriptions as source text. Record visual-only propositions and uncertainty "
    "in coverage_notes as limitations requiring independent review, not as text-grounded claims. "
    "The compiler has not received these notes or pixels through this context path.\n"
)


def context_path(run_root, reading_id):
    return safe_path(Path(run_root), f"derived-context/{reading_id}.json")


def prompt(context):
    return context["instruction"] + json.dumps(context["assets"], sort_keys=True, ensure_ascii=False)


def _freeze(path, value):
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"derived context drift: {path}; preserve history and start a new run/recollect evidence")
    else:
        atomic_write_json(path, value)


def load_context(path):
    """Missing and changed records are errors, not permission to silently drop context."""
    value = json.loads(Path(path).read_text())
    if value.get("sha256") != stable_revision({k: v for k, v in value.items() if k != "sha256"}):
        raise ValueError("derived context checksum mismatch; restore immutable record or recollect in a new run")
    return value


def packet_defects(packet, expected):
    if packet.get("derived_context") != expected:
        return ["derived context differs or is missing; recollect evidence with current inspection records"]
    return []


def _asset_identity(relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or not path.parts:
        raise ValueError(f"unsafe source asset path: {relative}")
    return path


def _selection(asset, spec):
    original = asset.get("original_path", "").removeprefix("original/")
    if not original:
        return None  # Unresolved/remote assets have no local identity.
    identity = _asset_identity(original)
    matches = [row for row in spec["assets"] if _asset_identity(row["path"]) == identity]
    if len(matches) > 1:
        raise ValueError(f"ambiguous visual inspection input: {original}")
    if matches and matches[0]["sha256"] != asset.get("original_sha256"):
        raise ValueError(f"visual inspection/source bundle identity drift: {original}")
    return matches[0] if matches else None


def prepare_contexts(plan, bundles, run_root, spec, inspect, policy=None):
    """Inspect one original raster per request, then route via the frozen asset closure.

    A single multi-image answer cannot safely be split by assignment, so each captured
    record is source-asset scoped. The existing inspector still owns all transport.
    """
    policy = {"max_tokens": 4096, "required": False, **(policy or {})}
    if set(policy) != {"max_tokens", "required"} or type(policy["required"]) is not bool:
        raise ValueError("visual_context requires max_tokens and boolean required")
    if type(policy["max_tokens"]) is not int or policy["max_tokens"] < 1:
        raise ValueError("visual_context max_tokens must be positive")
    from .sources import _reading_tokenizer

    encoding, tokenizer = _reading_tokenizer()
    results, records = {}, {}
    for reading in plan.data["readings"]:
        assets = []
        for ref in reading["assets"]:
            bundle = bundles[ref["source_file"]]
            asset = bundle["assets"][ref["index"]]
            row = {**ref, "bundle_key": bundle["key"], "source_asset": asset, "inspection": None}
            selected = _selection(asset, spec) if spec else None
            row["limitation"] = "inspection not requested" if not spec else "no matching inspection input"
            if selected:
                single = {**spec, "assets": [selected]}
                key = stable_revision(single)
                if key not in records:
                    records[key] = inspect(single, key)
                record = records[key]
                if len(record["assets"]) != 1:
                    raise ValueError("reading context requires a single-asset inspection record")
                inspected = record["assets"][0]
                if (inspected["path"], inspected["sha256"], inspected["derivative_sha256"]) != (
                    selected["path"],
                    asset["original_sha256"],
                    asset.get("derivative_sha256"),
                ):
                    raise ValueError("inspection original/derivative differs from frozen source bundle")
                row["inspection"] = record
                row["limitation"] = record["unresolved"] or (
                    "Unreviewed interpretation; visual-only propositions lack textual quotation grounding."
                )
                if policy["required"] and (record["unresolved"] or not inspected["inspected"]):
                    raise ValueError("required inspection unresolved; preserve record and retry in a new run")
            elif policy["required"]:
                raise ValueError("required inspection missing; supply matching visual spec")
            assets.append(row)
        context = {
            "version": 1,
            "reading_id": reading["id"],
            "instruction": INSTRUCTION,
            "policy": policy,
            "tokenizer": tokenizer,
            "assets": assets,
            "compiler_received": False,
        }
        context["tokens"] = len(encoding.encode(prompt(context), disallowed_special=()))
        if context["tokens"] > policy["max_tokens"]:
            raise ValueError("derived-context budget exceeded; increase visual_context.max_tokens in a new run")
        context["sha256"] = stable_revision(context)
        _freeze(context_path(run_root, reading["id"]), context)
        results[reading["id"]] = context
    return results
