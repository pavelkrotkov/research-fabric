"""Stable claim identity and durable claim transition primitives."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from contextlib import suppress


class ClaimIdentityError(ValueError):
    """Raised when a packet or repair transition cannot be bound safely."""


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_revision(value) -> str:
    """Return a deterministic revision for a JSON-compatible value."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory_source_entries(value):
    root = pathlib.Path(value)
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    return sorted(_file_digest(p) for p in paths)


def _manifest_source_entries(value):
    entries = [row.get("sha256") for row in value or [] if isinstance(row, dict)]
    return sorted(entries, key=lambda digest: digest or "")


def _source_entries(value) -> list[str | None]:
    if isinstance(value, (str, pathlib.Path)):
        return _directory_source_entries(value)
    return _manifest_source_entries(value)


def source_revision(value) -> str:
    """Hash source bytes or manifest digests used by a report and repair run.

    Source identity and representation binding remain the caller's concern;
    this revision only binds the bytes used by a report and a repair run.
    """
    return stable_revision(_source_entries(value))


def source_file_revision(source_dir: pathlib.Path, source_file: str) -> str:
    """Return the digest for one packet source, rejecting path escapes."""
    root = pathlib.Path(source_dir).resolve()
    relative = pathlib.PurePath(str(source_file))
    if relative.is_absolute() or ".." in relative.parts:
        raise ClaimIdentityError(f"claim source file escapes source root: {source_file!r}")
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ClaimIdentityError(f"claim source file is unavailable: {source_file!r}")
    return _file_digest(path)


def claim_id_for(worker: str, ordinal: int) -> str:
    """Allocate the stable ID used when a host accepts packet ordinal ``ordinal``."""
    if not isinstance(worker, str) or not worker or ordinal < 1:
        raise ClaimIdentityError(f"invalid claim identity inputs: worker={worker!r}, ordinal={ordinal!r}")
    return f"c-{worker}-{ordinal}"


def _claims(parsed: dict) -> list[dict]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
        raise ClaimIdentityError("packet has no claims list")
    if any(not isinstance(claim, dict) for claim in parsed["claims"]):
        raise ClaimIdentityError("packet contains a non-object claim")
    return parsed["claims"]


def validate_claim_ids(claims: list[dict]) -> None:
    seen = set()
    for claim in claims:
        cid = claim.get("claim_id")
        if not isinstance(cid, str) or not cid.strip():
            raise ClaimIdentityError("accepted claim is missing claim_id")
        if cid in seen:
            raise ClaimIdentityError(f"duplicate claim_id: {cid}")
        seen.add(cid)


def packet_state_revision(parsed: dict) -> str:
    """Hash the current claim records, including order and mutable excerpts."""
    return stable_revision({"claims": _claims(parsed)})


def _lineage_revision(worker: str, claims: list[dict]) -> str:
    return stable_revision({"worker": worker, "claims": claims})


def _bind_claim_source(claim: dict, source_dir: pathlib.Path, source_bindings: dict) -> None:
    source_file = claim.get("source_file", "")
    digest = source_file_revision(source_dir, source_file)
    claim_id = claim["claim_id"]
    binding = source_bindings.get(claim_id)
    if binding and (binding.get("source_file") != source_file or binding.get("source_revision") != digest):
        raise ClaimIdentityError(f"claim source binding changed for {claim_id}")
    source_bindings[claim_id] = {"source_file": source_file, "source_revision": digest}
    prior = claim.get("source_revision")
    if prior and prior != digest:
        raise ClaimIdentityError(f"source revision mismatch for claim {claim_id}")
    claim["source_revision"] = digest


def _prepare_claims(claims, worker, source_dir, source_bindings, attempt_id):
    for ordinal, claim in enumerate(claims, 1):
        claim.setdefault("claim_id", claim_id_for(worker, ordinal))
        _bind_claim_source(claim, source_dir, source_bindings)
        if attempt_id:
            claim.setdefault("accepted_attempt_id", attempt_id)
    validate_claim_ids(claims)


def _record_acceptance(packet: dict, claims: list[dict], initialized: bool) -> None:
    history = packet.setdefault("claim_history", [])
    if initialized:
        return
    state_after = packet["packet_state_revision"]
    history.extend(
        {
            "event": "accepted",
            "claim_id": claim["claim_id"],
            "packet_revision": packet["packet_revision"],
            "packet_state_before": None,
            "packet_state_after": state_after,
            "source_revision": claim.get("source_revision"),
            "source_file": claim.get("source_file"),
            "old_excerpt": None,
            "new_excerpt": claim.get("excerpt", ""),
            "reason": "accepted extracted claim",
            "attempt_id": claim.get("accepted_attempt_id"),
        }
        for claim in claims
    )


def _claim_ids(claims: list[dict]) -> set[str | None]:
    return {claim.get("claim_id") for claim in claims}


def _validate_identity_bindings(packet, original_ids):
    if not isinstance(original_ids, list):
        raise ClaimIdentityError("packet claim identity has no immutable original claim order")
    validate_claim_ids([{"claim_id": cid} for cid in original_ids])
    legacy_map = packet.get("legacy_claim_id_map", {})
    targets = list(legacy_map.values())
    if len(targets) != len(set(targets)) or not set(targets).issubset(original_ids):
        raise ClaimIdentityError("legacy ID map has ambiguous or unknown target identities")
    bindings = packet.get("claim_source_bindings")
    if not isinstance(bindings, dict) or not set(original_ids).issubset(bindings):
        raise ClaimIdentityError("accepted packet is missing original source bindings")


def validated_drop_ids(packet: dict) -> set[str]:
    """Return only explicit drops bound to this packet and its accepted sources."""
    dropped = set()
    bindings = packet.get("claim_source_bindings", {})
    for event in packet.get("claim_history", []):
        if event.get("event") != "drop":
            continue
        cid = event.get("claim_id")
        binding = bindings.get(cid, {})
        expected = {
            "packet_revision": packet.get("packet_revision"),
            "source_file": binding.get("source_file"),
            "source_revision": binding.get("source_revision"),
            "new_excerpt": None,
        }
        if not binding or any(event.get(key) != value for key, value in expected.items()):
            raise ClaimIdentityError(f"drop event has incompatible packet/source identity: {cid}")
        _validate_drop_details(event)
        dropped.add(cid)
    return dropped


def _validate_drop_details(event):
    fields = ("reason", "attempt_id", "packet_state_before", "packet_state_after")
    if any(not isinstance(event.get(key), str) or not event[key].strip() for key in fields):
        raise ClaimIdentityError("drop event lacks reason, attempt, or state revisions")
    if not isinstance(event.get("old_excerpt"), str):
        raise ClaimIdentityError("drop event lacks the original excerpt")
    if event["packet_state_before"] == event["packet_state_after"]:
        raise ClaimIdentityError("drop event did not change packet state")


def _validate_persisted_identity(packet: dict, worker: str, claims: list[dict]) -> None:
    identity = packet.get("claim_identity")
    if not isinstance(identity, dict):
        raise ClaimIdentityError("packet revision has no persisted claim identity")
    if identity.get("version") != 1 or identity.get("worker") != worker:
        raise ClaimIdentityError("packet claim identity metadata is invalid")
    original_ids = identity.get("claim_ids")
    _validate_identity_bindings(packet, original_ids)
    validate_claim_ids(claims)
    current_ids = _claim_ids(claims)
    if not current_ids.issubset(set(original_ids)):
        raise ClaimIdentityError("packet contains a claim outside its accepted identity")
    missing = set(original_ids) - current_ids
    dropped = validated_drop_ids(packet)
    if missing != dropped:
        raise ClaimIdentityError("packet lost or restored claims without a valid explicit drop history")


def accept_packet(
    packet: dict,
    worker: str,
    *,
    source_dir: pathlib.Path,
    attempt_id: str | None = None,
) -> dict:
    """Bind accepted claims to durable IDs and immutable packet provenance.

    IDs are assigned only when a host accepts the packet. A packet with an
    existing lineage is never silently re-numbered; a missing ID in that state
    is a corruption error. The returned object is safe to persist atomically.
    """
    if not isinstance(packet, dict):
        raise ClaimIdentityError("packet is not a JSON object")
    parsed = packet.get("parsed", packet)
    claims = _claims(parsed)
    initialized = bool(packet.get("packet_revision"))
    if initialized:
        _validate_persisted_identity(packet, worker, claims)
    elif any(claim.get("claim_id") for claim in claims):
        raise ClaimIdentityError("packet contains claim IDs before host identity was persisted")
    bindings = packet.setdefault("claim_source_bindings", {})
    _prepare_claims(claims, worker, source_dir, bindings, attempt_id)
    if not initialized:
        packet["packet_revision"] = _lineage_revision(worker, claims)
        packet["claim_identity"] = {
            "version": 1,
            "worker": worker,
            "claim_ids": [claim["claim_id"] for claim in claims],
        }
        packet["legacy_claim_id_map"] = {}
    packet["packet_state_revision"] = packet_state_revision(parsed)
    _record_acceptance(packet, claims, initialized)
    packet["source_revision"] = source_revision(source_dir)
    return packet


def _validate_legacy_record(row, claim, source_dir):
    fields = ("claim", "excerpt", "locator", "stance", "source_file")
    if any(row.get(field) != claim.get(field) for field in fields):
        raise ClaimIdentityError("legacy packet records differ from original ledger")
    if row.get("source_revision") != source_file_revision(source_dir, claim.get("source_file", "")):
        raise ClaimIdentityError("legacy migration requires source revision from the retained original source ledger")


def migrate_legacy_packet(packet: dict, worker: str, original_ledger: list[dict], *, source_dir: pathlib.Path) -> dict:
    """Bind a legacy packet to its retained original published ledger.

    The caller must supply the original artifact, never a ledger reconstructed
    from the current packet. A shortened, reordered, or repaired legacy packet
    cannot be safely recovered without its original per-record correspondence.
    """
    claims = _claims(packet.get("parsed", packet))
    rows = [row for row in original_ledger if str(row.get("claim_id", "")).startswith(f"c-{worker}-")]
    expected_ids = [claim_id_for(worker, ordinal) for ordinal in range(1, len(claims) + 1)]
    if [row.get("claim_id") for row in rows] != expected_ids:
        raise ClaimIdentityError("legacy packet order/count differs from original ledger")
    for row, claim in zip(rows, claims):
        _validate_legacy_record(row, claim, source_dir)
    accepted = accept_packet(packet, worker, source_dir=source_dir)
    accepted["legacy_claim_id_map"] = dict(zip(expected_ids, expected_ids))
    accepted["legacy_original_state"] = accepted["packet_state_revision"]
    accepted["legacy_ledger_revision"] = stable_revision(rows)
    return accepted


def resolve_claim_id(packet: dict, reported_id: str) -> str | None:
    """Resolve a current or explicitly mapped legacy report ID."""
    claims = _claims(packet.get("parsed", packet))
    if reported_id in _claim_ids(claims):
        return reported_id
    mapped = packet.get("legacy_claim_id_map") or {}
    if reported_id in mapped:
        return mapped[reported_id]
    if any(item.get("claim_id") == reported_id for item in packet.get("claim_history", [])):
        return reported_id
    return None


def _history_match(packet: dict, claim_id: str, report_id: str | None) -> bool:
    if not report_id:
        return False
    return any(
        item.get("report_id") == report_id and item.get("claim_id") == claim_id
        for item in packet.get("claim_history", [])
    )


def _transition_record(parsed, claims, claim, action, new_excerpt):
    old_excerpt = claim.get("excerpt", "")
    if action == "repair":
        if not isinstance(new_excerpt, str) or not new_excerpt.strip():
            raise ClaimIdentityError(f"repair for {claim['claim_id']} has no excerpt")
        claim["excerpt"] = new_excerpt
    else:
        parsed["claims"] = [item for item in claims if item.get("claim_id") != claim["claim_id"]]
    return old_excerpt


def _transition_claim_record(claims, claim_id):
    matches = [claim for claim in claims if claim.get("claim_id") == claim_id]
    if not matches:
        raise ClaimIdentityError(f"unknown claim_id: {claim_id}")
    if len(matches) != 1:
        raise ClaimIdentityError(f"duplicate claim_id: {claim_id}")
    return matches[0]


def transition_claim(
    packet: dict,
    claim_id: str,
    *,
    action: str,
    reason: str,
    attempt_id: str | None = None,
    report_id: str | None = None,
    new_excerpt: str | None = None,
) -> tuple[dict, dict | None]:
    """Apply one ID-addressed repair/drop transition and record its audit row."""
    if action not in {"repair", "drop"}:
        raise ClaimIdentityError(f"unsupported claim transition: {action}")
    parsed = packet.get("parsed", packet)
    claims = _claims(parsed)
    if _history_match(packet, claim_id, report_id):
        return packet, None
    claim = _transition_claim_record(claims, claim_id)
    before = packet.get("packet_state_revision") or packet_state_revision(parsed)
    old_excerpt = _transition_record(parsed, claims, claim, action, new_excerpt)
    after = packet_state_revision(parsed)
    packet["packet_state_revision"] = after
    event = {
        "event": action,
        "claim_id": claim_id,
        "packet_revision": packet.get("packet_revision"),
        "packet_state_before": before,
        "packet_state_after": after,
        "source_revision": claim.get("source_revision"),
        "source_file": claim.get("source_file"),
        "old_excerpt": old_excerpt,
        "new_excerpt": new_excerpt if action == "repair" else None,
        "reason": reason,
        "attempt_id": attempt_id,
    }
    if report_id:
        event["report_id"] = report_id
    packet.setdefault("claim_history", []).append(event)
    return packet, event


def _atomic_write_bytes(path: pathlib.Path, payload: bytes) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def atomic_write_bytes(path: pathlib.Path, value: bytes) -> None:
    """Replace a file atomically while preserving the previous valid file."""
    _atomic_write_bytes(path, value)


def atomic_write_text(path: pathlib.Path, value: str) -> None:
    """Replace UTF-8 text atomically while preserving the previous file."""
    atomic_write_bytes(path, value.encode("utf-8"))


def atomic_write_json(path: pathlib.Path, value) -> None:
    """Write JSON without exposing a partially-written packet to a reader."""
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _load_history(path: pathlib.Path) -> list[dict]:
    if pathlib.Path(path).exists():
        raw = pathlib.Path(path).read_text(encoding="utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return value if isinstance(value, list) else []
    return []


def append_history(path: pathlib.Path, entries: list[dict]) -> None:
    """Atomically append audit entries, retaining the last valid history."""
    if not entries:
        return
    rows = _load_history(path)
    known = {stable_revision(row) for row in rows}
    rows.extend(row for row in entries if stable_revision(row) not in known)
    atomic_write_json(path, rows)
