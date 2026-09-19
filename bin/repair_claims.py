"""Repair failed excerpts by stable claim identity.

The grounding report binds each target to a packet revision and source
revision. All targets are resolved and validated before the first packet is
written. Packet writes use ``os.replace`` through the shared core helper, so a
failed write leaves the last valid packet intact.

Usage: repair_claims.py <run_root> <source_dir> <grounding_report.txt>
    --field-root <isolated-field-root> --project <project.yaml> [--alignment <alignment.jsonl>]
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric._source_adapter import ADAPTERS
from research_fabric.claim_report import Report
from research_fabric.claim_store import PacketStore
from research_fabric.claim_validation import preflight, revalidate
from research_fabric.claim_validation import source_body as _source_body
from research_fabric.claims import (
    ClaimIdentityError,
)
from research_fabric.execution import InvalidOutput, worker_session


def call_model(prompt, session):
    def validate(text):
        value = extract_json(text)
        if not isinstance(value, dict) or not isinstance(value.get("found"), bool):
            raise InvalidOutput()
        if value["found"] and not isinstance(value.get("excerpt"), str):
            raise InvalidOutput()
        return text

    return session.call([{"role": "user", "content": prompt}], validate=validate)


def extract_json(text):
    """Decode whole JSON first, then find a repair object inside fences or prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _scan_json_object(text)


def _scan_json_object(text):
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "found" in value:
            return value
    raise ValueError("model response did not contain a repair JSON object")


def _replacement(result, body, grounded):
    if not result.get("found"):
        return "drop", None, "passage not found in source"
    excerpt = result.get("excerpt", "")
    if not excerpt or not grounded(excerpt, body):
        return "drop", None, "model excerpt was not verbatim in source"
    return "repair", excerpt, "model supplied a grounded replacement excerpt"


def _repair_target(target, source_dir, model, grounded, adapters):
    claim = target.claim
    body = _source_body(source_dir, claim, adapters)
    prompt = (
        f"You are repairing evidence claim {target.claim_id}. Its excerpt was rejected because it is not a verbatim "
        "substring of the source text. Preserve the claim text and stance.\n\n"
        f"CLAIM: {claim.get('claim', '')}\nREJECTED EXCERPT: {claim.get('excerpt', '')}\n\n"
        'Reply with ONLY JSON: {"excerpt":"<exact source substring>","found":true}. '
        'If the passage does not exist, reply {"found":false}.\n\nSOURCE TEXT:\n' + body
    )
    result = extract_json(model(prompt))
    if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
        raise ValueError("repair response requires a boolean found field")
    return _replacement(result, body, grounded)


def _process_targets(targets, store, source_dir, report_id, model, grounded, adapters, run_root, project):
    """Commit each completed target before continuing; aggregate model errors afterward.

    Persistence failures propagate immediately; replay reads the last durable
    packet to determine which targets still need repair.
    """
    counts = {"repair": 0, "drop": 0}
    errors = []
    for number, target in enumerate(targets, 1):
        if target.applied:
            continue
        session = None
        callback = model
        if callback is None:
            session = worker_session(
                run_root, "repair", target.claim_id, [source_dir / target.claim["source_file"]], project
            )
            def callback(prompt):
                return call_model(prompt, session)
        try:
            action, excerpt, reason = _repair_target(target, source_dir, callback, grounded, adapters)
        except Exception as exc:
            errors.append(f"{target.claim_id}: model error {exc}")
            continue
        if session:
            target.packet.setdefault("execution", []).extend(session.records())
        event = store.commit(
            target,
            action=action,
            new_excerpt=excerpt,
            reason=reason,
            attempt_id=f"{report_id}:{target.claim_id}:call-{number}",
            report_id=report_id,
        )
        if event:
            counts[event["event"]] += 1
    if errors:
        raise ClaimIdentityError("repair did not resolve every target: " + "; ".join(errors))
    return counts


def repair_claims(
    run_root: pathlib.Path,
    source_dir: pathlib.Path,
    report_path: pathlib.Path,
    model=None,
    *,
    field_root: pathlib.Path | None = None,
    acceptance: dict | None = None,
    project: dict | None = None,
    alignment_path: pathlib.Path | None = None,
    adapters=ADAPTERS,
):
    """Repair one bound report with the supplied provider callback, then re-gate.

    A library call without ``field_root`` validates packets only and returns
    ``fully_validated=False``. The CLI requires a field root and project spec
    so provenance, excerpt and applicable translation gates also run. Neither
    mode materializes the repaired publication ledger; the workflow does that.

    Report/identity errors fail before calls or writes. Once repair starts,
    successful earlier targets remain durable if a later model call or gate
    fails. Their audit permits resume without repeating or retargeting them.
    The callback receives a prompt and returns response text; execution policy
    and provider provenance remain the configurable-execution boundary.
    """
    from excerpt_grounding import grounded

    run_root = pathlib.Path(run_root)
    source_dir = pathlib.Path(source_dir)
    store = PacketStore(run_root / "evidence", source_dir, adapters)
    report = Report.read(pathlib.Path(report_path), store.packets, source_dir)
    targets = report.targets(store.packets)
    preflight(field_root, project)
    counts = _process_targets(
        targets, store, source_dir, report.report_id, model, grounded, adapters, run_root, project
    )
    fully_validated = revalidate(
        run_root,
        source_dir,
        store.packets,
        field_root,
        project or {"acceptance": acceptance or {}},
        alignment_path,
        grounded,
        adapters,
    )
    return {
        "repaired": counts["repair"],
        "dropped": counts["drop"],
        "report_id": report.report_id,
        "fully_validated": fully_validated,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=pathlib.Path)
    parser.add_argument("source_dir", type=pathlib.Path)
    parser.add_argument("report", type=pathlib.Path)
    parser.add_argument("--field-root", type=pathlib.Path, required=True)
    parser.add_argument("--project", type=pathlib.Path, required=True)
    parser.add_argument("--alignment", type=pathlib.Path)
    args = parser.parse_args()
    from research_fabric.core import load_project

    try:
        project = load_project(args.project.parent, args.project.stem)
    except RuntimeError as exc:
        parser.error(str(exc))
    repair_claims(
        args.run_root,
        args.source_dir,
        args.report,
        field_root=args.field_root,
        acceptance=project.get("acceptance") or {},
        project=project,
        alignment_path=args.alignment,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
