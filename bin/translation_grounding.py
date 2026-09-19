#!/usr/bin/env python3
"""Deterministic translation-grounding gate (Aeneid multi-witness).

Verifies each claim's selected English witness (an interpretive rendering of a
canonical Latin passage) WITHOUT trusting LLM judgement:

- english_witness.source_id must exist in the source ledger AND be a
  `translation`-role source (never masquerade as authoritative evidence).
- english_witness.excerpt must occur verbatim (transcription-tolerant) in that
  witness's adapter representation.
- The witness's locator must be consistent with the alignment: the quoted
  English must come from the witness book aligned to the claim's canonical
  Latin locator.
- translator / source_id metadata must be coherent.

The gate is fail-closed: a claim that fails any check is listed and the run is
blocked (exit != 0). The LLM's *choice* of which witness is "best" remains
reviewable judgement and is recorded, not hard-gated here.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from bin.excerpt_grounding import grounded, source_text  # noqa: E402


def _aligned_books(alignment_path: pathlib.Path) -> dict:
    """Load alignment.jsonl -> {(canonical_book, witness_variant): aligned canonical range}."""
    out = {}
    for line in alignment_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cb = row["book"]
        for variant, meta in (row.get("witnesses") or {}).items():
            out[(cb, variant)] = meta.get("aligned_to") or f"Aen.{cb}.1-"
    return out


def _canonical_book_of(locator: str) -> int | None:
    m = re.match(r"\bAen\.\s*(\d+)\.", str(locator))
    return int(m.group(1)) if m else None


def _witness_book_of(locator: str, variant: str) -> int | None:
    m = re.match(r"\s*(\d{1,2})\s*[.;:-]", str(locator))
    if m:
        return int(m.group(1))
    m2 = re.match(r"\s*BOOK\s+([IVX]+)", str(locator), re.I)
    if m2:
        rn = {
            "I": 1,
            "II": 2,
            "III": 3,
            "IV": 4,
            "V": 5,
            "VI": 6,
            "VII": 7,
            "VIII": 8,
            "IX": 9,
            "X": 10,
            "XI": 11,
            "XII": 12,
        }
        return rn.get(m2.group(1).upper())
    return None


def check(claims, sources_by_id, snap_text_by_id, alignment):
    failures = []
    for claim in claims:
        ew = claim.get("english_witness")
        cid = claim.get("claim_id")
        if not isinstance(ew, dict):
            failures.append((cid, "no english_witness object"))
            continue
        wsid = ew.get("source_id")
        wloc = ew.get("locator")
        wex = ew.get("excerpt")
        wtr = ew.get("translator")
        src = sources_by_id.get(wsid)
        if not src:
            failures.append((cid, f"english_witness source_id {wsid!r} not in source ledger"))
            continue
        if src.get("role") != "translation":
            failures.append((cid, f"english_witness source {wsid} is not a translation witness"))
        variant = src.get("translator")
        if not variant:
            failures.append((cid, f"english_witness source {wsid} has no translator metadata"))
        snap_text = snap_text_by_id.get(wsid, "")
        if not snap_text:
            failures.append((cid, f"english_witness snapshot missing for {wsid}"))
            continue
        if not isinstance(wex, str) or not grounded(wex, snap_text):
            failures.append((cid, f"english_witness excerpt not verbatim in {wsid}"))
        cbook = _canonical_book_of(claim.get("locator"))
        wbook = _witness_book_of(wloc, variant or "") if isinstance(wloc, str) else None
        if wbook is None:
            failures.append((cid, f"english_witness locator {wloc!r} unparseable"))
            continue
        suffix = variant.lower()
        aligned = alignment.get((cbook, variant)) or alignment.get((cbook, suffix))
        if aligned is None:
            failures.append((cid, f"no alignment for book {cbook} witness {variant}"))
            continue
        if wbook != cbook:
            failures.append((cid, f"english_witness locator book {wbook} != canonical book {cbook}"))
        known = {s.get("translator") for s in sources_by_id.values() if s.get("role") == "translation"}
        if wtr and known and wtr not in known:
            failures.append((cid, f"english_witness translator {wtr!r} not in translation sources"))
    return failures


def _source_text(field_root: pathlib.Path, row: dict) -> str:
    snapshot = field_root / row["snapshot"]
    return source_text(snapshot)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: translation_grounding.py <field_root> [alignment.jsonl]", file=sys.stderr)
        print(json.dumps({"self_test": "needs-field-root"}), file=sys.stderr)
        return 2
    field_root = pathlib.Path(sys.argv[1]).resolve()
    alignment_path = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    evidence = field_root / "evidence"
    if not (evidence / "sources.jsonl").is_file() or not (evidence / "claims.jsonl").is_file():
        print(json.dumps({"error": "missing evidence/sources.jsonl or claims.jsonl", "valid": False}), file=sys.stderr)
        return 1
    sources = [json.loads(line) for line in (evidence / "sources.jsonl").read_text().splitlines() if line.strip()]
    claims = [json.loads(line) for line in (evidence / "claims.jsonl").read_text().splitlines() if line.strip()]
    if not any(c.get("english_witness") for c in claims):
        print(json.dumps({"claims": len(claims), "multi_witness": 0, "valid": True}))
        return 0
    sources_by_id = {r["source_id"]: r for r in sources}
    snap_text_by_id = {r["source_id"]: _source_text(field_root, r) for r in sources if r.get("role") == "translation"}
    if alignment_path is None:
        for cand in (
            field_root / "evidence" / "alignment.jsonl",
            ROOT / "corpora" / "aeneid" / "alignment.jsonl",
        ):
            if cand.exists():
                alignment_path = cand
                break
    alignment = _aligned_books(alignment_path) if alignment_path and alignment_path.exists() else {}
    if not alignment:
        print(json.dumps({"claims": len(claims), "error": "no alignment available", "valid": False}), file=sys.stderr)
        return 1
    failures = check(claims, sources_by_id, snap_text_by_id, alignment)
    if failures:
        for cid, why in failures:
            print(f"{cid}: {why}", file=sys.stderr)
        print(
            json.dumps({"claims": len(claims), "ungrounded_witnesses": len(failures), "valid": False}), file=sys.stderr
        )
        return 1
    print(json.dumps({"claims": len(claims), "ungrounded_witnesses": 0, "valid": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
