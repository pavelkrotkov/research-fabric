#!/usr/bin/env python3
"""Deterministic validation for the custom research evidence ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.sources import representation_for  # noqa: E402

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
    if REPRESENTATION_KEYS.isdisjoint(row):
        return []
    missing = REPRESENTATION_KEYS - row.keys()
    if missing:
        return [f"source representation metadata missing {sorted(missing)}: {sid}"]
    try:
        rep = representation_for(snap)
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"source representation invalid: {sid}: {exc}"]
    expected = {
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
    }
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
    for line, row in source_rows:
        missing = SOURCE_KEYS - row.keys()
        if missing:
            errors.append(f"sources.jsonl:{line}: missing {sorted(missing)}")
        sid = row.get("source_id")
        if sid in source_ids:
            errors.append(f"duplicate source_id: {sid}")
        source_ids.add(sid)
        errors.extend(source_errors(root, row, sid))
    claim_ids = set()
    for line, row in claim_rows:
        missing = CLAIM_KEYS - row.keys()
        if missing:
            errors.append(f"claims.jsonl:{line}: missing {sorted(missing)}")
        cid = row.get("claim_id")
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
