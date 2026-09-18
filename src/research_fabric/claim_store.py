"""Packet-first durability: packet audit is authoritative; its sidecar is recoverable.

Each accepted packet contains its own transition history. Replacing that JSON
is the durable boundary; the separate history file is an audit mirror. If its
write is interrupted, the next validated report run reconstructs missing rows
from packets before continuing. No recovery path invents a new transition or
uses the mirror to select a repair target.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from contextlib import suppress

from ._source_adapter import ADAPTERS


def atomic_write_bytes(path: pathlib.Path, payload: bytes) -> None:
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
    from .claims import stable_revision

    rows = _load_history(path)
    known = {stable_revision(row) for row in rows}
    rows.extend(row for row in entries if stable_revision(row) not in known)
    atomic_write_json(path, rows)


class PacketStore:
    """Load verified packet identities and publish each transition packet before its mirror."""

    def __init__(self, directory, source_dir, adapters=ADAPTERS):
        from .claims import ClaimIdentityError, accept_packet, validate_claim_ids

        self.packets, self.paths = {}, {}
        self.history_path = directory / "claim-history.json"
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

    def recover(self):
        # Called only after the entire report passes validation. No untrusted report
        # can cause acceptance metadata or a recovered sidecar to be written.
        events = [
            event
            for packet in self.packets.values()
            for event in packet.get("claim_history", [])
            if event.get("event") in {"repair", "drop"} and event.get("report_id")
        ]
        append_history(self.history_path, events)
        for worker, packet in self.packets.items():
            atomic_write_json(self.paths[worker], packet)

    def commit(self, target, **change):
        from .claims import transition_claim

        packet, event = transition_claim(target.packet, target.claim_id, **change)
        atomic_write_json(self.paths[target.worker], packet)
        append_history(self.history_path, [event] if event else [])
        return event
