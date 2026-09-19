"""Grounding report v1: one binding protocol for production, repair and replay.

The deterministic gate hashes the complete envelope with a canonical digest,
including its failure records and every ledger packet's original/current
revision pair. Repair validates that envelope and resolves all targets before
any model call or write. A report may resume only through its own recorded
transitions, in order, from its original state to the current packet state.

Legacy prose is normalized once at the boundary, using explicit retained-
ledger migration metadata. Both formats then use the same target resolution,
unknown/duplicate checks, packet binding and replay rules. Neither path infers
a claim from its current list position.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass

from .claims import require, source_file_revision, source_revision, stable_revision

META_PREFIX = "REPORT-META:"
REVISION_FIELDS = ("packet_revision", "packet_state_revision")


def _packet_bindings(claims):
    packets = {}
    for claim in claims:
        binding = {key: claim.get(key) for key in REVISION_FIELDS}
        worker = claim.get("worker")
        if worker and all(binding.values()):
            previous = packets.setdefault(worker, binding)
            if previous != binding:
                previous["incompatible"] = True
    return packets


def grounding_report_metadata(claims, sources, failures):
    """Produce the v1 envelope from ledger rows and deterministic gate failures.

    Bind all represented packets, including those without failures. Conflicting
    revisions remain explicitly incompatible; the producer must not choose
    whichever row happens to occur last. Sources are original manifest rows.
    """
    packets = _packet_bindings(claims)
    by_id = {claim.get("claim_id"): claim for claim in claims}
    rows = [
        {
            "claim_id": cid,
            "worker": by_id.get(cid, {}).get("worker"),
            "reason": reason,
            "old_excerpt": by_id.get(cid, {}).get("excerpt"),
            "source_ids": by_id.get(cid, {}).get("source_ids", []),
        }
        for cid, reason in failures
    ]
    metadata = {"version": 1, "source_revision": source_revision(sources), "packets": packets, "failures": rows}
    names = [row.get("source_file") or pathlib.PurePosixPath(row.get("snapshot", "")).name for row in sources]
    if names and all(names):
        require(len(names) == len(set(names)), "grounding report source filenames are ambiguous")
        metadata["source_files"] = names
    return dict(metadata, report_id=stable_revision(metadata))


def _report_source_revision(metadata, source_dir):
    # Older reports without document names retain their strict whole-directory check.
    if "source_files" not in metadata:
        return source_revision(source_dir)
    names = metadata["source_files"]
    require(
        isinstance(names, list) and names and all(isinstance(name, str) and name for name in names),
        "grounding report source filenames are invalid",
    )
    require(len(names) == len(set(names)), "grounding report source filenames are ambiguous")
    return source_revision([{"sha256": source_file_revision(source_dir, name)} for name in names])


@dataclass(frozen=True)
class Target:
    worker: str
    packet: dict
    claim_id: str
    claim: dict | None
    applied: bool


def _identity_index(packets, report_id):
    index = {}
    for worker, packet in packets.items():
        claims = {claim["claim_id"]: claim for claim in packet["parsed"]["claims"]}
        applied = {e["claim_id"] for e in packet["claim_history"] if e.get("report_id") == report_id}
        aliases = {cid: cid for cid in packet["claim_identity"]["claim_ids"]}
        aliases.update(packet.get("legacy_claim_id_map", {}))
        for alias, cid in aliases.items():
            index.setdefault(alias, {})[worker] = Target(worker, packet, cid, claims.get(cid), cid in applied)
    return index


def _resolve(failure, index):
    require(
        isinstance(failure, dict) and isinstance(failure.get("claim_id"), str),
        "grounding report contains an invalid claim ID",
    )
    matches = index.get(failure["claim_id"], {})
    if failure.get("worker"):
        matches = {key: value for key, value in matches.items() if key == failure["worker"]}
    require(len(matches) == 1, f"grounding report claim ID is unknown or ambiguous: {failure['claim_id']}")
    return next(iter(matches.values()))


def _validate_binding(worker, binding, packets):
    require(isinstance(binding, dict), f"grounding report has invalid packet binding: {worker}")
    require(worker in packets, f"grounding report references unknown packet worker: {worker}")
    require(not binding.get("incompatible"), f"grounding report has incompatible revisions for packet {worker}")
    require(
        all(isinstance(binding.get(key), str) for key in REVISION_FIELDS),
        f"grounding report is missing packet revision fields: {worker}",
    )
    require(
        binding["packet_revision"] == packets[worker]["packet_revision"],
        f"grounding report packet revision is stale: {worker}",
    )


@dataclass(frozen=True)
class Report:
    report_id: str
    bindings: dict
    failures: list

    @classmethod
    def read(cls, path, packets, source_dir):
        """Read and normalize report syntax without writing packets or calling a model.

        Envelope integrity/source checks happen here. ``targets`` must then
        validate packet bindings and the entire replay chain before use.
        """
        raw = path.read_text(encoding="utf-8")
        envelopes = [line[len(META_PREFIX) :] for line in raw.splitlines() if line.startswith(META_PREFIX)]
        if not envelopes:
            return cls(*normalize_report(raw, packets))
        require(len(envelopes) == 1, "grounding report has multiple metadata envelopes")
        metadata = json.loads(envelopes[0])
        require(isinstance(metadata, dict), "grounding report metadata is not an object")
        require(metadata.get("version") == 1, "unsupported grounding report version")
        require(
            metadata.get("source_revision") == _report_source_revision(metadata, source_dir),
            "grounding report source revision is stale",
        )
        report_id = metadata.pop("report_id", None)
        require(report_id == stable_revision(metadata), "grounding report identity is invalid")
        return cls(report_id, metadata.get("packets"), metadata.get("failures"))

    def targets(self, packets):
        """Resolve all targets and non-target packet bindings before mutation.

        Returned targets retain references to the loaded packet/claim objects.
        They are a validated execution snapshot, not independent copies or a
        lock: use them serially with the same loaded packets. Applied targets
        are replay no-ops; no current list offset participates in resolution.
        """
        require(isinstance(self.bindings, dict), "grounding report is missing packet bindings")
        require(isinstance(self.failures, list), "grounding report failures are not a list")
        index = _identity_index(packets, self.report_id)
        targets = [self._target(failure, index) for failure in self.failures]
        ids = [target.claim_id for target in targets]
        require(len(ids) == len(set(ids)), "grounding report repeats claim ID")
        for worker, binding in self.bindings.items():
            _validate_binding(worker, binding, packets)
            self._state(worker, packets[worker], {t.claim_id for t in targets if t.worker == worker})
        return targets

    def _target(self, failure, index):
        target = _resolve(failure, index)
        require(
            isinstance(failure.get("worker"), str)
            and "old_excerpt" in failure
            and isinstance(failure.get("reason"), str),
            "grounding report failure is missing identity fields",
        )
        require(target.worker in self.bindings, f"grounding report is missing packet binding: {target.worker}")
        if not target.applied:
            require(target.claim is not None, f"grounding report claim ID is no longer present: {target.claim_id}")
            require(
                failure["old_excerpt"] is None or target.claim.get("excerpt") == failure["old_excerpt"],
                f"grounding report claim excerpt is stale: {target.claim_id}",
            )
        return target

    def _state(self, worker, packet, targets):
        # Replaying this report may advance its starting state only through its own targets.
        state = self.bindings[worker]["packet_state_revision"]
        for event in packet["claim_history"]:
            if event.get("report_id") == self.report_id:
                require(
                    event.get("claim_id") in targets and event.get("packet_state_before") == state,
                    f"grounding report packet state is stale: {worker}",
                )
                state = event.get("packet_state_after")
        require(state == packet["packet_state_revision"], f"grounding report packet state is stale: {worker}")


def normalize_report(raw, packets):
    """Convert old positional prose once, using only an explicit original-ledger migration."""
    report_id = stable_revision({"legacy_report": raw})
    index = _identity_index(packets, report_id)
    failures, bindings = [], {}
    for cid, reason in re.findall(r"^\s*(c-[^:\s]+):\s*(.+)$", raw, re.M):
        if "excerpt" not in reason and "snapshot" not in reason:
            continue
        target = _resolve({"claim_id": cid}, index)
        packet = target.packet
        require(
            packet.get("legacy_original_state") and packet.get("legacy_ledger_revision"),
            "legacy report requires migration bound to the original published ledger",
        )
        bindings[target.worker] = {
            "packet_revision": packet["packet_revision"],
            "packet_state_revision": packet["legacy_original_state"],
        }
        failures.append({"claim_id": cid, "worker": target.worker, "reason": reason, "old_excerpt": None})
    return report_id, bindings, failures
