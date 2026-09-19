"""Atomically persist claim transitions and their history in one authoritative packet."""

from __future__ import annotations

import json
import os
import pathlib
import uuid

from boltons.fileutils import atomic_save

from ._source_adapter import ADAPTERS


def atomic_write_text(path: pathlib.Path, value: str) -> None:
    """Delegate replacement to boltons, then durably record the new directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_save(os.fspath(path), part_file=f".{path.name}.{uuid.uuid4().hex}.part") as handle:
        handle.write(value.encode("utf-8"))
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def atomic_write_json(path: pathlib.Path, value) -> None:
    """Serialize packet/audit JSON through the same durable replacement boundary."""
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class PacketStore:
    """Load verified identities and atomically persist each packet with its transition history."""

    def __init__(self, directory, source_dir, adapters=ADAPTERS):
        from .claims import ClaimIdentityError, accept_packet, validate_claim_ids

        self.packets, self.paths = {}, {}
        for path in sorted(directory.glob("worker-*.json")):
            packet = json.loads(path.read_text(encoding="utf-8"))
            worker = str(packet.get("worker") or path.stem.removeprefix("worker-"))
            if not packet.get("packet_revision"):
                raise ClaimIdentityError(f"legacy packet {worker} has no persisted claim identity; accept it first")
            if worker in self.packets:
                raise ClaimIdentityError(f"duplicate packet worker: {worker}")
            self.packets[worker] = accept_packet(packet, worker, source_dir=source_dir, adapters=adapters)
            self.paths[worker] = path
        validate_claim_ids(
            [{"claim_id": cid} for packet in self.packets.values() for cid in packet["claim_identity"]["claim_ids"]]
        )

    def commit(self, target, **change):
        from .claims import transition_claim

        packet, event = transition_claim(target.packet, target.claim_id, **change)
        atomic_write_json(self.paths[target.worker], packet)
        return event
