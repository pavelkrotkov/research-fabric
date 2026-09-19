"""Accepted claim identity, source binding, and ID-addressed repair transitions.

The v1 packet has three different revisions. ``packet_revision`` identifies
its original accepted lineage and never changes; ``packet_state_revision``
hashes the current ordered claims; ``source_revision`` binds the report's
source byte set, not source names or adapter behavior. Per-claim bindings and
the existing source-attestation contract supply those latter guarantees.

``claim_identity.claim_ids`` and ``claim_source_bindings`` retain the original
records even after deletion. Missing live IDs must exactly equal validated
drop IDs: neither acceptance history nor a shortened list authorizes a drop.
Reordering and later witness annotations may change state without changing
identity. Repair itself changes only an excerpt, or removes one explicit ID.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from ._source_adapter import ADAPTERS
from .claim_store import atomic_write_json as atomic_write_json
from .claim_store import atomic_write_text as atomic_write_text


class ClaimIdentityError(ValueError):
    """A claim, source, or report cannot be bound safely."""


def require(condition, message):
    if not condition:
        raise ClaimIdentityError(message)


def stable_revision(value):
    """Hash canonical JSON with stable key ordering and UTF-8 encoding.

    This is an integrity fingerprint, not an authenticated signature.
    Changing its serialization would invalidate existing v1 report IDs.
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _directory_digests(root):
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()]


def source_revision(value):
    """Bind the source-byte multiset used by a report, preserving duplicate digests.

    Accept either a directory of source files or source-manifest rows. Paths
    are deliberately absent so a copied publication snapshot matches its run
    source. This does not prove filename identity or adapter equivalence;
    per-claim bindings and source attestations must establish those separately.
    """
    if isinstance(value, (str, pathlib.Path)):
        digests = _directory_digests(pathlib.Path(value))
    else:
        digests = [row.get("sha256") for row in value or [] if isinstance(row, dict)]
    return stable_revision(sorted(digests, key=lambda digest: digest or ""))


def source_file_revision(source_dir, source_file):
    """Hash one original source after rejecting traversal and symlink escapes.

    The digest attests original bytes, not the extracted representation. The
    canonical source adapter separately supplies the representation digest.
    """
    root = pathlib.Path(source_dir).resolve()
    relative = pathlib.PurePath(str(source_file))
    require(
        not relative.is_absolute() and ".." not in relative.parts,
        f"claim source file escapes source root: {source_file!r}",
    )
    path = (root / relative).resolve()
    require(root in path.parents and path.is_file(), f"claim source file is unavailable: {source_file!r}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def claim_id_for(worker, ordinal):
    """Allocate an ID from the original extraction order at first acceptance.

    Ordinals are one-based within the run's worker namespace.
    Callers must persist it, never invoke this to renumber a repaired packet.
    Equal claim text does not imply equal record identity.
    """
    require(isinstance(worker, str) and worker and ordinal >= 1, "invalid claim identity inputs")
    return f"c-{worker}-{ordinal}"


def _claims(packet):
    parsed = packet.get("parsed", packet)
    require(isinstance(parsed, dict) and isinstance(parsed.get("claims"), list), "packet has no claims list")
    require(all(isinstance(claim, dict) for claim in parsed["claims"]), "packet contains a non-object claim")
    return parsed["claims"]


def validate_claim_ids(claims):
    """Reject missing or repeated IDs without imposing semantic deduplication."""
    ids = [claim.get("claim_id") for claim in claims]
    require(all(isinstance(cid, str) and cid.strip() for cid in ids), "accepted claim is missing claim_id")
    require(len(ids) == len(set(ids)), "duplicate claim_id")


def packet_state_revision(parsed):
    """Hash the current ordered claim records, including excerpts and annotations.

    Packet metadata and audit history are excluded: recording a transition
    must not recursively change the state it describes.
    """
    return stable_revision({"claims": _claims(parsed)})


def _event(packet, claim, action, before, old_excerpt, new_excerpt, reason, attempt_id):
    """Build the shared v1 audit shape for acceptance, replacement and deletion.

    Acceptance rows all describe the same completed initial packet state;
    their previous state/excerpt are null. They are not incremental replay
    transitions. A drop instead has a null new excerpt and retains the old
    excerpt/source binding after the live claim is removed. Repair rows carry
    both excerpts and the packet states surrounding that single mutation.
    """
    return {
        "event": action,
        "claim_id": claim["claim_id"],
        "packet_revision": packet["packet_revision"],
        "packet_state_before": before,
        "packet_state_after": packet["packet_state_revision"],
        "source_revision": claim.get("source_revision"),
        "source_file": claim.get("source_file"),
        "old_excerpt": old_excerpt,
        "new_excerpt": new_excerpt,
        "reason": reason,
        "attempt_id": attempt_id,
    }


def validated_drop_ids(packet):
    """Validate auditable deletion evidence against the original packet/source binding.

    This checks the deletion record, not a second event-sourcing authority.
    Report replay separately verifies the applicable state-transition chain.
    """
    dropped = set()
    for event in packet.get("claim_history", []):
        if event.get("event") != "drop":
            continue
        cid = event.get("claim_id")
        binding = packet.get("claim_source_bindings", {}).get(cid, {})
        expected = dict(binding, packet_revision=packet.get("packet_revision"), new_excerpt=None)
        require(
            binding and all(event.get(key) == value for key, value in expected.items()),
            f"drop event has incompatible packet/source identity: {cid}",
        )
        _drop_details(event)
        dropped.add(cid)
    return dropped


def _drop_details(event):
    fields = ("reason", "attempt_id", "packet_state_before", "packet_state_after")
    require(
        all(isinstance(event.get(key), str) and event[key].strip() for key in fields),
        "drop event lacks reason, attempt, or state revisions",
    )
    require(isinstance(event.get("old_excerpt"), str), "drop event lacks the original excerpt")
    require(event["packet_state_before"] != event["packet_state_after"], "drop event did not change packet state")


def _identity(packet, worker, claims):
    identity = packet.get("claim_identity")
    require(isinstance(identity, dict), "packet revision has no persisted claim identity")
    require(
        identity.get("version") == 1 and identity.get("worker") == worker, "packet claim identity metadata is invalid"
    )
    original = identity.get("claim_ids")
    require(isinstance(original, list), "packet claim identity has no immutable original claim order")
    validate_claim_ids([{"claim_id": cid} for cid in original])
    validate_claim_ids(claims)
    current = {claim["claim_id"] for claim in claims}
    require(current.issubset(original), "packet contains a claim outside its accepted identity")
    _identity_bindings(packet, original)
    require(
        set(original) - current == validated_drop_ids(packet),
        "packet lost or restored claims without a valid explicit drop history",
    )


def _identity_bindings(packet, original):
    """Require aliases to be injective and confined to this accepted lineage.

    An old positional report may use only its saved mapping, never the current
    order. Two aliases cannot silently authorize the same target twice.
    """
    targets = list(packet.get("legacy_claim_id_map", {}).values())
    require(
        len(targets) == len(set(targets)) and set(targets).issubset(original),
        "legacy ID map has ambiguous or unknown target identities",
    )
    bindings = packet.get("claim_source_bindings")
    require(
        isinstance(bindings, dict) and set(original).issubset(bindings),
        "accepted packet is missing original source bindings",
    )


def _bind_sources(packet, claims, source_dir, attempt_id):
    """Reverify live claims against their original per-ID source bindings.

    Even byte-identical text in another file cannot authorize moving a claim:
    both its saved filename/digest binding and its own digest must still agree.
    """
    bindings = packet.setdefault("claim_source_bindings", {})
    for claim in claims:
        digest = source_file_revision(source_dir, claim.get("source_file", ""))
        binding = {"source_file": claim.get("source_file", ""), "source_revision": digest}
        cid = claim["claim_id"]
        require(bindings.get(cid, binding) == binding, f"claim source binding changed for {cid}")
        require(claim.get("source_revision", digest) == digest, f"source revision mismatch for claim {cid}")
        bindings[cid] = binding
        claim["source_revision"] = digest
        if attempt_id:
            claim.setdefault("accepted_attempt_id", attempt_id)


def _attested_coverage(packet, rows):
    names = [row.get("source_file", "") for row in rows]
    required = {claim.get("source_file", "") for claim in _claims(packet)}
    required.update(binding.get("source_file", "") for binding in packet.get("claim_source_bindings", {}).values())
    require(
        len(names) == len(set(names)) and required.issubset(names),
        "packet source provenance has duplicate or missing accepted sources",
    )


def _attestation(packet, source_dir, adapters):
    """Verify an extraction envelope without selecting a replacement input set.

    Rows can include context or witnesses no surviving claim cites. Verify all
    of them with the extraction registry rather than rebuilding provenance
    from survivors. An absent legacy envelope is distinct from a present empty
    one: absence uses compatibility, while an empty claim-bearing envelope fails.
    """
    from .sources import source_provenance, source_provenance_errors

    if "source_provenance" not in packet:
        return  # Explicit pre-adapter packet compatibility; never mint a replacement attestation.
    rows = packet["source_provenance"]
    require(
        isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
        "packet source provenance contains invalid entries",
    )
    _attested_coverage(packet, rows)
    paths = []
    for row in rows:
        name = row.get("source_file", "")
        source_file_revision(source_dir, name)
        paths.append(pathlib.Path(source_dir) / name)
    errors = source_provenance_errors(rows, source_provenance(paths, adapters))
    require(not errors, "; ".join(errors))


def accept_packet(packet, worker, *, source_dir, attempt_id=None, adapters=ADAPTERS):
    """Assign IDs once at host acceptance, mutating and returning the supplied packet.

    The host must run project/structural acceptance first. This layer checks
    identity and provenance, not minimum claim counts or research semantics.
    Callers own the input and must persist the returned packet atomically;
    failure is not a transaction that rolls back changes to the Python object.

    Existing identity is validated before live source bindings are refreshed.
    Present adapter attestations are reverified, including context/witness
    inputs; absent attestations remain an explicit pre-adapter compatibility
    path and are never fabricated here. Original source manifests still gate
    publication independently. This function makes no disk writes.
    """
    require(isinstance(packet, dict), "packet is not a JSON object")
    _attestation(packet, source_dir, adapters)
    claims = _claims(packet)
    initialized = packet.get("packet_revision")
    if initialized:
        _identity(packet, worker, claims)
    else:
        require(
            not any(claim.get("claim_id") for claim in claims),
            "packet contains claim IDs before host identity was persisted",
        )
        for ordinal, claim in enumerate(claims, 1):
            claim["claim_id"] = claim_id_for(worker, ordinal)
    _bind_sources(packet, claims, source_dir, attempt_id)
    packet["packet_state_revision"] = packet_state_revision(packet)
    if not initialized:
        packet["packet_revision"] = stable_revision({"worker": worker, "claims": claims})
        packet["claim_identity"] = {"version": 1, "worker": worker, "claim_ids": [c["claim_id"] for c in claims]}
        packet["legacy_claim_id_map"] = {}
        packet.setdefault("claim_history", []).extend(
            _event(
                packet,
                c,
                "accepted",
                None,
                None,
                c.get("excerpt", ""),
                "accepted extracted claim",
                c.get("accepted_attempt_id"),
            )
            for c in claims
        )
    packet["source_revision"] = source_revision(source_dir)
    return packet


def migrate_legacy_packet(packet, worker, original_ledger, *, source_dir, adapters=ADAPTERS):
    """Explicit compatibility entry point; normal host acceptance never enables positional reports."""
    from .claim_legacy import migrate_packet

    return migrate_packet(packet, worker, original_ledger, source_dir=source_dir, adapters=adapters)


def _applied(history, report_id, claim_id):
    return any(event.get("report_id") == report_id and event.get("claim_id") == claim_id for event in history)


def transition_claim(packet, claim_id, *, action, reason, attempt_id=None, report_id=None, new_excerpt=None):
    """Mutate an accepted packet's addressed record and return (packet, audit event).

    Supply the failed report ID and a nonempty attempt identity for auditable
    repair/drop operations. The reason describes this outcome, not a change
    to claim text or stance. No provider call or publication gate runs here.

    A previously applied report/claim pair is a no-op. Callers must bind and
    validate the complete report before invoking this primitive; PacketStore
    then persists the transition and its history together in the packet.
    """
    require(action in {"repair", "drop"}, f"unsupported claim transition: {action}")
    history = packet.setdefault("claim_history", [])
    if report_id and _applied(history, report_id, claim_id):
        return packet, None
    claims = _claims(packet)
    matches = [claim for claim in claims if claim.get("claim_id") == claim_id]
    require(len(matches) == 1, f"unknown or duplicate claim_id: {claim_id}")
    claim = matches[0]
    before, old_excerpt = packet_state_revision(packet), claim.get("excerpt", "")
    _replace_excerpt(packet, claim, action, new_excerpt)
    packet["packet_state_revision"] = packet_state_revision(packet)
    event = _event(
        packet, claim, action, before, old_excerpt, new_excerpt if action == "repair" else None, reason, attempt_id
    )
    if report_id:
        event["report_id"] = report_id
    history.append(event)
    return packet, event


def _replace_excerpt(packet, claim, action, excerpt):
    if action == "repair":
        require(isinstance(excerpt, str) and excerpt.strip(), f"repair for {claim['claim_id']} has no excerpt")
        claim["excerpt"] = excerpt
    else:
        packet.get("parsed", packet)["claims"] = [c for c in _claims(packet) if c["claim_id"] != claim["claim_id"]]
