"""Re-gate repaired packets in disposable evidence; never rewrite the publication tree.

Ledger synchronization is a validation projection, not a publication engine:
all live IDs must already occur in the supplied ledger and every omitted ID
must have an explicit validated drop. Surviving rows retain their original
order and publication-specific columns. Claim text, stance and source cannot
be silently replaced; excerpt and current bindings follow the accepted packet.

Packet/project acceptance and grounding run even without a field root. Full
validation additionally runs the real provenance, excerpt and applicable
translation CLI gates against a copied evidence tree. A failed gate preserves
the repair audit but cannot change the original ledger or authorize publication.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

from ._source_adapter import ADAPTERS
from .claims import (
    ClaimIdentityError,
    accept_packet,
    atomic_write_text,
    require,
    source_file_revision,
    validated_drop_ids,
)
from .core import multisource_packet_defects, packet_defects
from .sources import representation_for

ROOT = pathlib.Path(__file__).resolve().parents[2]


def source_body(source_dir, claim, adapters=ADAPTERS):
    # Containment is checked before the adapter opens a packet-controlled path.
    source_file_revision(source_dir, claim.get("source_file", ""))
    try:
        return representation_for(source_dir / claim["source_file"], adapters).text
    except ValueError as exc:
        raise ClaimIdentityError(str(exc)) from exc


def read_ledger(path):
    require(path.is_file(), f"missing ledger: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    require(all(isinstance(row, dict) for row in rows), f"ledger row is not an object: {path}")
    return rows


def preflight(field_root, project):
    if field_root is not None:
        require(project is not None, "full repair validation requires the project spec")
        read_ledger(pathlib.Path(field_root) / "evidence" / "claims.jsonl")
        require((pathlib.Path(field_root) / "evidence" / "sources.jsonl").is_file(), "missing source ledger")


def _packet_index(packets):
    current, drops = {}, set()
    for packet in packets.values():
        drops.update(validated_drop_ids(packet))
        for claim in packet["parsed"]["claims"]:
            cid = claim.get("claim_id")
            require(isinstance(cid, str) and cid, "packet contains an invalid claim ID")
            require(cid not in current, f"duplicate accepted claim ID: {cid}")
            current[cid] = (packet, claim)
    return current, drops


def _ledger_claim(row, packet, claim):
    for key in ("claim", "stance", "source_file"):
        require(key not in row or row[key] == claim.get(key), f"ledger {key} changed for {claim['claim_id']}")
    # Historical execution provenance survives when an older packet omits it.
    # Witness annotations differ: absence in the repaired packet explicitly clears
    # a prior optional annotation so the translation gate sees the current claim.
    result = dict(row, excerpt=claim.get("excerpt", ""))
    result.update({key: packet.get(key) for key in ("packet_revision", "packet_state_revision")})
    result.update({key: claim[key] for key in claim.keys() & {"source_revision", "accepted_attempt_id"}})
    fields = {"claim_type", "english_witness", "witnesses_consulted"}
    result.update({key: claim.get(key) for key in (claim.keys() | row.keys()) & fields})
    return result


def sync_ledger_rows(rows, packets):
    current, drops = _packet_index(packets)
    ids = [row.get("claim_id") for row in rows]
    require(all(isinstance(cid, str) and cid for cid in ids), "ledger contains a missing claim_id")
    require(len(ids) == len(set(ids)), "ledger repeats claim_id")
    require(set(current).issubset(ids), f"ledger is missing accepted claim IDs: {sorted(set(current) - set(ids))}")
    require((set(ids) - set(current)).issubset(drops), "ledger claim is outside accepted packet history")
    return [_ledger_claim(row, *current[row["claim_id"]]) for row in rows if row["claim_id"] in current]


def _packet_gate(packets, source_dir, project, grounded, adapters):
    acceptance = project.get("acceptance") or {}
    for worker, packet in packets.items():
        parsed = packet.get("parsed")
        defects = (
            multisource_packet_defects(parsed, project, acceptance)
            if project.get("witnesses")
            else packet_defects(parsed, acceptance)
        )
        require(not defects, f"packet {worker} failed project acceptance: {'; '.join(defects)}")
        accepted = accept_packet(packet, worker, source_dir=source_dir, adapters=adapters)
        for claim in accepted["parsed"]["claims"]:
            require(
                grounded(claim.get("excerpt", ""), source_body(source_dir, claim, adapters)),
                f"surviving claim is still ungrounded: {claim['claim_id']}",
            )


def _gate(script, field_root, *extra):
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / script), str(field_root), *map(str, extra)], capture_output=True, text=True
    )
    require(not result.returncode, f"post-repair {script} failed: {(result.stderr or result.stdout).strip()[-500:]}")


def revalidate(run_root, source_dir, packets, field_root, project, alignment, grounded, adapters=ADAPTERS):
    _packet_gate(packets, source_dir, project, grounded, adapters)
    if field_root is None:
        return False
    evidence = pathlib.Path(field_root).resolve() / "evidence"
    rows = sync_ledger_rows(read_ledger(evidence / "claims.jsonl"), packets)
    with tempfile.TemporaryDirectory(prefix="research-fabric-revalidate-") as temporary:
        candidate = pathlib.Path(temporary) / "field"
        shutil.copytree(evidence, candidate / "evidence")
        atomic_write_text(
            candidate / "evidence" / "claims.jsonl",
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        )
        _gate("provenance_validate.py", candidate)
        _gate("excerpt_grounding.py", candidate)
        _translation_gate(rows, run_root, candidate, alignment)
    return True


def _translation_gate(rows, run_root, candidate, alignment):
    if not any(row.get("english_witness") for row in rows):
        return
    options = [run_root / "alignment.jsonl", candidate / "evidence" / "alignment.jsonl"]
    if alignment:
        options.insert(0, pathlib.Path(alignment))
    available = next((path for path in options if path.is_file()), None)
    require(available is not None, "post-repair translation grounding lacks alignment input")
    _gate("translation_grounding.py", candidate, available)
