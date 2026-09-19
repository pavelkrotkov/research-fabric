#!/usr/bin/env python3
"""Deterministic validation for the custom research evidence ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.sources import published_reading_plan, representation_for, source_attestation  # noqa: E402

SOURCE_KEYS = {"source_id", "url", "title", "retrieved_at", "content_type", "sha256", "snapshot"}
REPRESENTATION_KEYS = {"adapter", "adapter_version", "representation_encoding", "representation_sha256"}
CLAIM_KEYS = {
    "claim_id",
    "claim",
    "note",
    "source_ids",
    "locator",
    "excerpt",
    "stance",
    "confidence",
    "independence_group",
    "verified_at",
}
STANCES = {"supports", "qualifies", "contradicts"}


def load_jsonl(path: pathlib.Path):
    rows = []
    if not path.exists():
        raise ValueError(f"missing {path}")
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append((n, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: invalid JSON: {exc}") from exc
    return rows


def _representation_errors(row: dict, sid, snap: pathlib.Path) -> list[str]:
    if REPRESENTATION_KEYS.isdisjoint(row) and snap.suffix.lower() not in {".md", ".markdown"}:
        return []
    missing = REPRESENTATION_KEYS - row.keys()
    if missing:
        return [f"source representation metadata missing {sorted(missing)}: {sid}"]
    try:
        rep = representation_for(snap)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"source representation invalid: {sid}: {exc}"]
    expected = source_attestation(rep)
    del expected["source_file"], expected["sha256"]
    expected["assets_sha256"] = rep.assets_sha256
    if rep.source_metadata or "source_metadata" in row:
        expected["source_metadata"] = dict(rep.source_metadata)
    return [f"{key} mismatch: {sid}" for key, value in expected.items() if row.get(key) != value]


def source_errors(root: pathlib.Path, row: dict, sid) -> list[str]:
    snap = (root / row.get("snapshot", "")).resolve()
    if root not in snap.parents:
        return [f"source snapshot escapes root: {sid}"]
    if not snap.is_file():
        return [f"missing snapshot: {sid}: {snap}"]
    errors = []
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    if digest != row.get("sha256"):
        errors.append(f"sha256 mismatch: {sid}")
    return errors + _representation_errors(row, sid, snap)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("field_root", type=pathlib.Path)
    args = ap.parse_args()
    root = args.field_root.resolve()
    evidence = root / "evidence"
    errors = []
    source_rows = load_jsonl(evidence / "sources.jsonl")
    claim_rows = load_jsonl(evidence / "claims.jsonl")
    source_ids = set()
    sources_by_id = {}
    for line, row in source_rows:
        missing = SOURCE_KEYS - row.keys()
        if missing:
            errors.append(f"sources.jsonl:{line}: missing {sorted(missing)}")
        sid = row.get("source_id")
        if sid in source_ids:
            errors.append(f"duplicate source_id: {sid}")
        source_ids.add(sid)
        sources_by_id[sid] = row
        errors.extend(source_errors(root, row, sid))
    reading_plan = None
    if not errors:
        try:
            reading_plan = published_reading_plan(root, [row for _, row in source_rows])
        except (ValueError, RuntimeError, KeyError) as exc:
            errors.append(f"reading plan invalid: {exc}")
    claim_ids = set()
    for line, row in claim_rows:
        if "reading" in row:
            try:
                if reading_plan is None:
                    raise ValueError("reading claim has no verified frozen plan")
                reading_plan.validate_claim(row)
                expected_group = row["reading"]["work_id"] if row["reading"]["role"] == "primary" else None
                if row.get("independence_group") != expected_group:
                    raise ValueError("reading claim independence group differs from assigned role/work")
            except (ValueError, KeyError) as exc:
                errors.append(f"reading claim invalid: {exc}")
        elif reading_plan:
            errors.append("claim has no frozen reading assignment")
        missing = CLAIM_KEYS - row.keys()
        if missing:
            errors.append(f"claims.jsonl:{line}: missing {sorted(missing)}")
        cid = row.get("claim_id")
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"missing claim_id: claims.jsonl:{line}")
        if cid in claim_ids:
            errors.append(f"duplicate claim_id: {cid}")
        claim_ids.add(cid)
        if row.get("stance") not in STANCES:
            errors.append(f"bad stance: {cid}")
        if not isinstance(row.get("source_ids"), list) or not row["source_ids"]:
            errors.append(f"claim has no source_ids: {cid}")
        for sid in row.get("source_ids", []):
            if sid not in source_ids:
                errors.append(f"unknown source_id {sid} in claim {cid}")
        source_revision = row.get("source_revision")
        if source_revision is not None:
            cited_revisions = {
                sources_by_id[sid].get("sha256") for sid in row.get("source_ids", []) if sid in sources_by_id
            }
            if source_revision not in cited_revisions:
                errors.append(f"source revision mismatch: {cid}")
        for field in ("packet_revision", "packet_state_revision", "accepted_attempt_id"):
            if field in row and (not isinstance(row[field], str) or not row[field].strip()):
                errors.append(f"bad {field}: {cid}")
        confidence = row.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"bad confidence: {cid}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(json.dumps({"sources": len(source_rows), "claims": len(claim_rows), "valid": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
