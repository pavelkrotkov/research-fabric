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
import os
import pathlib
import sys
import time

from openai import OpenAI

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric._source_adapter import ADAPTERS
from research_fabric.claim_report import Report
from research_fabric.claim_store import PacketStore
from research_fabric.claim_validation import preflight, revalidate
from research_fabric.claim_validation import source_body as _source_body
from research_fabric.claims import (
    ClaimIdentityError,
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _client():
    key = os.environ.get("OPENROUTER_API_KEY")
    envfile = os.environ.get("RESEARCH_FABRIC_ENV_FILE") or str(pathlib.Path.home() / ".hermes" / ".env")
    if not key and pathlib.Path(envfile).is_file():
        for line in pathlib.Path(envfile).read_text(errors="replace").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not available: set the env var or provide a .env-style file via "
            "RESEARCH_FABRIC_ENV_FILE"
        )
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)


def call_model(prompt):
    client = _client()
    for attempt in range(1, 16):
        try:
            reply = client.chat.completions.create(
                model="stealth/ox-alpha",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=20000,
            )
            return reply.choices[0].message.content
        except Exception as exc:
            text = str(exc)
            if "429" in text or "Provider returned error" in text or any(f"5{x}" in text[:4] for x in "01234"):
                time.sleep(min(60, 6 * attempt))
            else:
                raise
    raise RuntimeError("model call failed after 15 attempts")


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


def _process_targets(targets, store, source_dir, report_id, model, grounded, adapters):
    counts = {"repair": 0, "drop": 0}
    errors = []
    for number, target in enumerate(targets, 1):
        if target.applied:
            continue
        try:
            action, excerpt, reason = _repair_target(target, source_dir, model, grounded, adapters)
        except Exception as exc:
            errors.append(f"{target.claim_id}: model error {exc}")
            continue
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
    model=call_model,
    *,
    field_root: pathlib.Path | None = None,
    acceptance: dict | None = None,
    project: dict | None = None,
    alignment_path: pathlib.Path | None = None,
    adapters=ADAPTERS,
):
    """Repair one report, then revalidate the surviving packet/ledger evidence."""
    from excerpt_grounding import grounded

    run_root = pathlib.Path(run_root)
    source_dir = pathlib.Path(source_dir)
    store = PacketStore(run_root / "evidence", source_dir, adapters)
    report = Report.read(pathlib.Path(report_path), store.packets, source_dir)
    targets = report.targets(store.packets)
    preflight(field_root, project)
    store.recover()
    counts = _process_targets(targets, store, source_dir, report.report_id, model, grounded, adapters)
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
