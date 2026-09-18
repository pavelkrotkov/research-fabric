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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

from openai import OpenAI

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from research_fabric.claims import (
    ClaimIdentityError,
    accept_packet,
    append_history,
    atomic_write_json,
    atomic_write_text,
    source_file_revision,
    source_revision,
    stable_revision,
    transition_claim,
    validated_drop_ids,
)

META_PREFIX = "REPORT-META:"
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
    text = (text or "").strip()
    candidates = [text]
    candidates.extend(re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except json.JSONDecodeError:
            pass
    return _scan_json_object(text, decoder)


def _scan_json_object(text, decoder):
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


def _legacy_failures(raw: str) -> list[dict]:
    failures = []
    for line in raw.splitlines():
        match = re.match(r"(c-[^:\s]+):\s*(.+)$", line.strip())
        if match and ("excerpt" in match.group(2) or "snapshot" in match.group(2)):
            failures.append({"claim_id": match.group(1), "reason": match.group(2)})
    return failures


def _report_metadata(raw: str):
    for line in raw.splitlines():
        if line.startswith(META_PREFIX):
            value = json.loads(line[len(META_PREFIX) :].strip())
            if not isinstance(value, dict):
                raise ClaimIdentityError("grounding report metadata is not an object")
            return value
    return None


def _report(path: pathlib.Path) -> tuple[dict | None, list[dict], str]:
    raw = path.read_text(encoding="utf-8")
    metadata = _report_metadata(raw)
    if metadata is None:
        # Compatibility for ADR-006 reports written before revision metadata.
        # The packet's explicit legacy map makes this positional interpretation safe.
        failures = _legacy_failures(raw)
        return None, failures, stable_revision({"legacy_report": raw})
    failures = metadata.get("failures") or []
    if not isinstance(failures, list):
        raise ClaimIdentityError("grounding report failures are not a list")
    return metadata, failures, metadata.get("report_id") or stable_revision(metadata)


def _packet_worker(path: pathlib.Path, packet: dict) -> str:
    return str(packet.get("worker") or path.stem.removeprefix("worker-"))


def _validate_report_header(metadata, current_source_revision, packets):
    if metadata is None:
        return {}
    if metadata.get("version") != 1:
        raise ClaimIdentityError("unsupported grounding report version")
    if metadata.get("source_revision") != current_source_revision:
        raise ClaimIdentityError("grounding report source revision is stale")
    report_copy = dict(metadata)
    report_copy.pop("report_id", None)
    if metadata.get("report_id") != stable_revision(report_copy):
        raise ClaimIdentityError("grounding report identity is invalid")
    bindings = metadata.get("packets")
    if not isinstance(bindings, dict):
        raise ClaimIdentityError("grounding report is missing packet bindings")
    for worker, binding in bindings.items():
        _validate_packet_binding(worker, binding, packets)
    return bindings


def _validate_packet_binding(worker, binding, packets):
    if not isinstance(binding, dict):
        raise ClaimIdentityError(f"grounding report has invalid packet binding: {worker}")
    packet = packets.get(worker)
    if packet is None:
        raise ClaimIdentityError(f"grounding report references unknown packet worker: {worker}")
    if binding.get("incompatible"):
        raise ClaimIdentityError(f"grounding report has incompatible revisions for packet {worker}")
    if not isinstance(binding.get("packet_revision"), str) or not isinstance(binding.get("packet_state_revision"), str):
        raise ClaimIdentityError(f"grounding report is missing packet revision fields: {worker}")
    if binding["packet_revision"] != packet.get("packet_revision"):
        raise ClaimIdentityError(f"grounding report packet revision is stale: {worker}")


def _validate_failure_shape(failure, versioned):
    if not isinstance(failure, dict) or not isinstance(failure.get("claim_id"), str):
        raise ClaimIdentityError("grounding report contains an invalid claim ID")
    if versioned and (
        not isinstance(failure.get("worker"), str)
        or "old_excerpt" not in failure
        or not isinstance(failure.get("reason"), str)
    ):
        raise ClaimIdentityError("grounding report failure is missing identity fields")


def _report_identity_index(packets, report_id):
    """Resolve aliases, live claims, and completed report transitions once."""
    index = defaultdict(dict)
    for worker, packet in packets.items():
        claims = {claim["claim_id"]: claim for claim in packet["parsed"]["claims"]}
        applied = {
            event["claim_id"] for event in packet.get("claim_history", []) if event.get("report_id") == report_id
        }
        aliases = {cid: cid for cid in packet["claim_identity"]["claim_ids"]}
        aliases.update(packet.get("legacy_claim_id_map", {}))
        for reported, cid in aliases.items():
            index[reported][worker] = (packet, cid, claims.get(cid), cid in applied)
    return index


def _validate_target_excerpt(failure, claim, already, reported):
    old_excerpt = failure.get("old_excerpt")
    if not already and claim is None:
        raise ClaimIdentityError(f"grounding report claim ID is no longer present: {reported}")
    if not already and old_excerpt is not None and claim.get("excerpt") != old_excerpt:
        raise ClaimIdentityError(f"grounding report claim excerpt is stale: {reported}")


def _target_from_failure(failure, bindings, index, versioned):
    _validate_failure_shape(failure, versioned)
    reported = failure["claim_id"]
    matches = index.get(reported, {})
    if failure.get("worker"):
        matches = {worker: value for worker, value in matches.items() if worker == failure["worker"]}
    if len(matches) != 1:
        raise ClaimIdentityError(f"grounding report claim ID is unknown or ambiguous: {reported}")
    worker, (packet, cid, claim, already) = next(iter(matches.items()))
    if versioned and worker not in bindings:
        raise ClaimIdentityError(f"grounding report is missing packet binding: {worker}")
    _validate_target_excerpt(failure, claim, already, reported)
    return worker, packet, cid, already, claim


def _validate_packet_state(worker, binding, packet, target_ids, report_id):
    """Replay only this report's transitions from its original packet state."""
    state = binding["packet_state_revision"]
    events = [event for event in packet.get("claim_history", []) if event.get("report_id") == report_id]
    for event in events:
        if event.get("claim_id") not in target_ids or event.get("packet_state_before") != state:
            raise ClaimIdentityError(f"grounding report packet state is stale: {worker}")
        state = event.get("packet_state_after")
    if state != packet.get("packet_state_revision"):
        raise ClaimIdentityError(f"grounding report packet state is stale: {worker}")


def _legacy_report_bindings(packets, targets):
    """Old reports are usable only against their original migration state."""
    bindings = {}
    for worker in {target[1] for target in targets}:
        packet = packets[worker]
        original = packet.get("legacy_original_state")
        if not original or not packet.get("legacy_ledger_revision"):
            raise ClaimIdentityError("legacy report requires migration bound to the original published ledger")
        bindings[worker] = {"packet_state_revision": original}
    return bindings


def _validate_targets(metadata, failures, report_id, packets, current_source_revision):
    bindings = _validate_report_header(metadata, current_source_revision, packets)
    index = _report_identity_index(packets, report_id)
    grouped = defaultdict(set)
    targets = []
    seen = set()
    for failure in failures:
        worker, packet, cid, already, claim = _target_from_failure(failure, bindings, index, metadata is not None)
        if cid in seen:
            raise ClaimIdentityError(f"grounding report repeats claim ID: {failure['claim_id']}")
        seen.add(cid)
        grouped[worker].add(cid)
        targets.append((failure, worker, packet, cid, already, claim))
    if metadata is None:
        bindings = _legacy_report_bindings(packets, targets)
    for worker, binding in bindings.items():
        _validate_packet_state(worker, binding, packets[worker], grouped[worker], report_id)
    return targets


def _html_text(raw: str) -> str:
    import html
    from html.parser import HTMLParser

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            if tag in ("br", "p", "div", "li"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip = max(0, self.skip - 1)

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    parser = Text()
    parser.feed(raw)
    body = html.unescape("".join(parser.parts))
    body = re.sub(r"[ \t]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n\n", body)


def _source_body(source_dir: pathlib.Path, claim: dict) -> str:
    source_file = claim.get("source_file", "")
    source_file_revision(source_dir, source_file)
    path = source_dir / source_file
    if path.suffix.lower() not in {".html", ".htm", ".xhtml"}:
        raise ClaimIdentityError(f"unsupported source representation for repair: {path.name}")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ClaimIdentityError(f"source is not valid UTF-8 HTML: {path.name}") from exc
    return _html_text(raw)


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        raise ClaimIdentityError(f"missing ledger: {path}")
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClaimIdentityError(f"invalid ledger JSON at {path}:{number}") from exc
        if not isinstance(row, dict):
            raise ClaimIdentityError(f"ledger row is not an object at {path}:{number}")
        rows.append(row)
    return rows


def _index_packet_claims(worker, packet, current):
    parsed = packet.get("parsed") or {}
    for claim in parsed.get("claims", []):
        cid = claim.get("claim_id")
        if not isinstance(cid, str) or not cid:
            raise ClaimIdentityError(f"packet {worker} contains an invalid claim ID")
        if cid in current:
            raise ClaimIdentityError(f"duplicate accepted claim ID: {cid}")
        current[cid] = (worker, packet, claim)


def _packet_claim_index(packets):
    current = {}
    drop_ids = set()
    for worker, packet in packets.items():
        _index_packet_claims(worker, packet, current)
        drop_ids.update(validated_drop_ids(packet))
    return current, drop_ids


def _validate_ledger_identity(row, claim):
    for field in ("claim", "stance", "source_file"):
        if field in row and row[field] != claim.get(field):
            raise ClaimIdentityError(f"ledger {field} changed for {claim['claim_id']}")


def _update_ledger_metadata(updated, claim, packet):
    for field in ("packet_revision", "packet_state_revision"):
        updated[field] = packet.get(field)
    for field in ("source_revision", "accepted_attempt_id"):
        if field in claim:
            updated[field] = claim[field]
    for field in ("claim_type", "english_witness", "witnesses_consulted"):
        if field in updated or field in claim:
            updated[field] = claim.get(field)


def _update_ledger_row(row, claim, packet):
    _validate_ledger_identity(row, claim)
    updated = dict(row)
    updated["excerpt"] = claim.get("excerpt", "")
    _update_ledger_metadata(updated, claim, packet)
    return updated


def _sync_ledger_row(row, current, drop_ids, seen):
    cid = row.get("claim_id")
    if not isinstance(cid, str) or not cid:
        raise ClaimIdentityError("ledger contains a missing claim_id")
    if cid in seen:
        raise ClaimIdentityError(f"ledger repeats claim_id: {cid}")
    seen.add(cid)
    record = current.get(cid)
    if record is None:
        if cid in drop_ids:
            return None
        raise ClaimIdentityError(f"ledger claim is outside accepted packet history: {cid}")
    _worker, packet, claim = record
    return _update_ledger_row(row, claim, packet)


def _sync_ledger_rows(rows, packets):
    current, drop_ids = _packet_claim_index(packets)
    seen = set()
    synced = []
    for row in rows:
        updated = _sync_ledger_row(row, current, drop_ids, seen)
        if updated is not None:
            synced.append(updated)
    missing = set(current) - seen
    if missing:
        raise ClaimIdentityError(f"ledger is missing accepted claim IDs: {sorted(missing)}")
    return synced


def _validate_project_packets(packets, project):
    project = project or {}
    from research_fabric.core import multisource_packet_defects, packet_defects

    acceptance = project.get("acceptance") or {}
    validator = multisource_packet_defects if project.get("witnesses") else packet_defects
    for worker, packet in packets.items():
        defects = (
            validator(packet.get("parsed"), project, acceptance)
            if project.get("witnesses")
            else validator(packet.get("parsed"), acceptance)
        )
        if defects:
            raise ClaimIdentityError(f"packet {worker} failed project acceptance: {'; '.join(defects)}")


def _validate_packet_repair(packets, source_dir, acceptance, project):
    from excerpt_grounding import grounded

    _validate_project_packets(packets, project or {"acceptance": acceptance or {}})
    for worker, packet in packets.items():
        accepted = accept_packet(packet, worker, source_dir=source_dir)
        claims = (accepted.get("parsed") or {}).get("claims", [])
        for claim in claims:
            body = _source_body(source_dir, claim)
            if not grounded(claim.get("excerpt", ""), body):
                raise ClaimIdentityError(f"surviving claim is still ungrounded: {claim['claim_id']}")


def _run_gate(script, field_root, *extra):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "bin" / script), str(field_root), *map(str, extra)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ClaimIdentityError(f"post-repair {script} failed: {detail[-500:]}")


def _translation_alignment(run_root, field_root, alignment_path):
    if alignment_path:
        candidate = pathlib.Path(alignment_path)
        if candidate.is_file():
            return candidate
    for candidate in (run_root / "alignment.jsonl", field_root / "evidence" / "alignment.jsonl"):
        if candidate.is_file():
            return candidate
    return None


def _revalidate_ledger(
    run_root,
    source_dir,
    packets,
    field_root,
    acceptance,
    project,
    alignment_path,
):
    _validate_packet_repair(packets, source_dir, acceptance, project)
    if field_root is None:
        return False
    field_root = pathlib.Path(field_root).resolve()
    claims_path = field_root / "evidence" / "claims.jsonl"
    ledger_rows = _read_jsonl(claims_path)
    synced = _sync_ledger_rows(ledger_rows, packets)
    with tempfile.TemporaryDirectory(prefix="research-fabric-revalidate-") as temp_name:
        candidate_root = pathlib.Path(temp_name) / "field"
        shutil.copytree(field_root / "evidence", candidate_root / "evidence")
        atomic_write_text(
            candidate_root / "evidence" / "claims.jsonl",
            "\n".join(json.dumps(row, ensure_ascii=False) for row in synced) + "\n",
        )
        _run_gate("provenance_validate.py", candidate_root)
        _run_gate("excerpt_grounding.py", candidate_root)
        if any(row.get("english_witness") for row in synced):
            alignment = _translation_alignment(run_root, candidate_root, alignment_path)
            if alignment is None:
                raise ClaimIdentityError("post-repair translation grounding lacks alignment input")
            _run_gate("translation_grounding.py", candidate_root, alignment)
    return True


def _recover_history(history_path, packets):
    entries = []
    for packet in packets.values():
        entries.extend(
            event
            for event in packet.get("claim_history", [])
            if isinstance(event, dict) and event.get("event") in {"repair", "drop"} and event.get("report_id")
        )
    append_history(history_path, entries)


def _preflight_validation(field_root, project):
    if field_root is None:
        return
    if project is None:
        raise ClaimIdentityError("full repair validation requires the project spec")
    root = pathlib.Path(field_root)
    _read_jsonl(root / "evidence" / "claims.jsonl")
    if not (root / "evidence" / "sources.jsonl").is_file():
        raise ClaimIdentityError(f"missing source ledger: {root / 'evidence' / 'sources.jsonl'}")


def _transition_from_result(packet, cid, report_id, attempt_id, result, body, grounded):
    if not result.get("found"):
        return transition_claim(
            packet,
            cid,
            action="drop",
            reason="passage not found in source",
            attempt_id=attempt_id,
            report_id=report_id,
        )
    excerpt = result.get("excerpt", "")
    if not excerpt or not grounded(excerpt, body):
        return transition_claim(
            packet,
            cid,
            action="drop",
            reason="model excerpt was not verbatim in source",
            attempt_id=attempt_id,
            report_id=report_id,
        )
    return transition_claim(
        packet,
        cid,
        action="repair",
        reason="model supplied a grounded replacement excerpt",
        attempt_id=attempt_id,
        report_id=report_id,
        new_excerpt=excerpt,
    )


def _apply_target(target, source_dir, packet_path, history_path, report_id, number, model, grounded):
    _failure, _worker, packet, cid, already, claim = target
    if already:
        return None, None
    body = _source_body(source_dir, claim)
    prompt = (
        f"You are repairing evidence claim {cid}. Its excerpt was rejected because it is not a verbatim "
        "substring of the source text. Preserve the claim text and stance.\n\n"
        f"CLAIM: {claim.get('claim', '')}\nREJECTED EXCERPT: {claim.get('excerpt', '')}\n\n"
        'Reply with ONLY JSON: {"excerpt":"<exact source substring>","found":true}. '
        'If the passage does not exist, reply {"found":false}.\n\nSOURCE TEXT:\n' + body
    )
    try:
        result = extract_json(model(prompt))
        if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
            raise ValueError("repair response requires a boolean found field")
    except Exception as exc:
        print(f"{cid}: model error {exc}")
        return None, f"{cid}: model error {exc}"
    attempt_id = f"{report_id}:{cid}:call-{number}"
    packet, event = _transition_from_result(packet, cid, report_id, attempt_id, result, body, grounded)
    atomic_write_json(packet_path, packet)
    append_history(history_path, [event] if event else [])
    return event, None


def _event_counts(event):
    if not event:
        return 0, 0
    return (1, 0) if event["event"] == "repair" else (0, 1)


def _load_packets(packet_dir, source_dir):
    packets = {}
    paths = {}
    for path in sorted(packet_dir.glob("worker-*.json")):
        packet = json.loads(path.read_text(encoding="utf-8"))
        worker = _packet_worker(path, packet)
        identity = packet.get("claim_identity")
        if not packet.get("packet_revision") or not (isinstance(identity, dict) or packet.get("legacy_claim_id_map")):
            raise ClaimIdentityError(
                f"legacy packet {worker} has no persisted claim identity or explicit ID map; "
                "host acceptance is required before repair"
            )
        if worker in packets:
            raise ClaimIdentityError(f"duplicate packet worker: {worker}")
        packets[worker] = accept_packet(packet, worker, source_dir=source_dir)
        paths[worker] = path
    return packets, paths


def _persist_packets(packets, paths):
    for worker, packet in packets.items():
        atomic_write_json(paths[worker], packet)


def _process_targets(targets, source_dir, paths, history_path, report_id, model, grounded):
    repaired = dropped = 0
    errors = []
    for number, target in enumerate(targets, 1):
        _failure, worker, _packet, cid, _already, _claim = target
        event, error = _apply_target(
            target, source_dir, paths[worker], history_path, report_id, number, model, grounded
        )
        if error:
            errors.append(error)
        repaired_delta, dropped_delta = _event_counts(event)
        repaired += repaired_delta
        dropped += dropped_delta
        if event:
            print(f"{cid}: {'repaired' if event['event'] == 'repair' else 'dropped'}")
    return repaired, dropped, errors


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
):
    """Repair one report, then revalidate the surviving packet/ledger evidence."""
    from excerpt_grounding import grounded

    run_root = pathlib.Path(run_root)
    source_dir = pathlib.Path(source_dir)
    metadata, failures, report_id = _report(pathlib.Path(report_path))
    packet_dir = run_root / "evidence"
    packets, paths = _load_packets(packet_dir, source_dir)
    targets = _validate_targets(metadata, failures, report_id, packets, source_revision(source_dir))
    _preflight_validation(field_root, project)
    history_path = packet_dir / "claim-history.json"
    _recover_history(history_path, packets)

    # Persist legacy ID migration only after all report checks pass.
    _persist_packets(packets, paths)
    repaired, dropped, errors = _process_targets(targets, source_dir, paths, history_path, report_id, model, grounded)
    if errors:
        raise ClaimIdentityError("repair did not resolve every target: " + "; ".join(errors))
    fully_validated = _revalidate_ledger(run_root, source_dir, packets, field_root, acceptance, project, alignment_path)
    print(f"\ndone: repaired={repaired}, dropped={dropped}")
    return {
        "repaired": repaired,
        "dropped": dropped,
        "report_id": report_id,
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
