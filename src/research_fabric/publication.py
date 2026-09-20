"""Shared post-compilation publication for production and offline rehearsal."""

import json

from research_fabric.compilation import write_json


def _reading_projection(reading_plan, claim):
    return reading_plan.project_claim(claim) if reading_plan else {}


def _claim_row(field_root, worker, packet, claim, notes, sources, multi, reading_plan):
    source_id = sources.get(claim.get("source_file", ""))
    if not source_id:
        return None
    note = notes[source_id]
    if not (field_root / note).is_file():
        raise RuntimeError(f"claim note target does not exist: {note}")
    claim_id = claim.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        return None
    row = {
        "claim_id": claim_id,
        "worker": worker,
        "claim": claim.get("claim", ""),
        "note": note,
        "source_ids": [source_id],
        "source_file": claim.get("source_file", ""),
        "locator": claim.get("locator", ""),
        "excerpt": claim.get("excerpt", ""),
        "stance": claim.get("stance", "supports"),
        "confidence": claim.get("confidence", 0.0),
        "independence_group": claim.get("independence_group", worker),
        "verified_at": "pilot-verifier-pass",
        "packet_revision": packet.get("packet_revision"),
        "packet_state_revision": packet.get("packet_state_revision"),
        "source_revision": claim.get("source_revision"),
    }
    if claim.get("accepted_attempt_id"):
        row["accepted_attempt_id"] = claim["accepted_attempt_id"]
    if multi:
        row.update(
            claim_type=claim.get("claim_type", ""),
            english_witness=claim.get("english_witness"),
            witnesses_consulted=claim.get("witnesses_consulted", []),
        )
    row.update(_reading_projection(reading_plan, claim))
    return row


def ledger_rows(field_root, packets, notes, sources, *, multi=False, reading_plan=None):
    claims, dropped = [], []
    for worker, packet in packets.items():
        for index, claim in enumerate((packet.get("parsed") or {}).get("claims", []), 1):
            row = _claim_row(field_root, worker, packet, claim, notes, sources, multi, reading_plan)
            if row is None:
                dropped.append({"worker": worker, "index": index, "source_file": claim.get("source_file", "")})
            else:
                claims.append(row)
    return claims, dropped


def _packet_history(field_root, packets):
    execution = {
        worker: {"packet_revision": packet["packet_revision"], "execution": packet.get("execution")}
        for worker, packet in packets.items()
    }
    write_json(field_root / "evidence/packet-execution.json", execution)
    history = [event for packet in packets.values() for event in (packet.get("claim_history") or [])]
    if history:
        write_json(field_root / "evidence/claim-history.json", history)


def materialize_evidence(
    field_root, run_root, worker_ids, notes, sources, source_rows, *, multi=False, reading_plan=None
):
    """Project accepted packets and bound source rows without gating or committing."""
    packets = {
        worker: json.loads((run_root / "evidence" / f"worker-{worker}.json").read_text(encoding="utf-8"))
        for worker in worker_ids
    }
    claims, dropped = ledger_rows(field_root, packets, notes, sources, multi=multi, reading_plan=reading_plan)
    if dropped:
        write_json(run_root / "verification/dropped-claims.json", dropped)
        raise RuntimeError(f"{len(dropped)} claim(s) could not be mapped to a manifest source; see dropped-claims.json")
    if not claims:
        raise RuntimeError("no claims survived materialization")
    for filename, rows in (("claims.jsonl", claims), ("sources.jsonl", source_rows)):
        (field_root / "evidence" / filename).write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
        )
    _packet_history(field_root, packets)
    return claims
