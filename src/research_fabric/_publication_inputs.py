"""Validate prepared publication inputs and materialize immutable snapshots.

The publisher accepts only already accepted evidence and a frozen source set.
This module revalidates packet/source revisions, binds the manifest again at the
publication boundary, copies immutable snapshots, and derives native note names.
It does not compile, decide gate policy, or create Git commits.
"""

import json
import pathlib

from ._source_adapter import ADAPTERS
from .claims import accept_packet
from .compilation import write_json
from .sources import bind_manifest, copy_source_snapshot, preserve_original_bytes, snapshot_relative


def _revisions(packet, worker):
    keys = ("packet_revision", "packet_state_revision", "source_revision")
    revisions = {key: packet.get(key) for key in keys}
    if not all(revisions.values()):
        raise RuntimeError(f"publication requires accepted packet metadata: {worker}")
    return revisions


def _accepted_packets(run_root, worker_ids, source_dir, reading_plan, acceptance):
    packets = {}
    for worker in worker_ids:
        path = run_root / "evidence" / f"worker-{worker}.json"
        packet = json.loads(path.read_text(encoding="utf-8"))
        revisions = _revisions(packet, worker)
        if reading_plan:
            reading_plan.validate_packet(packet, worker, acceptance)
            from .derived_context import context_path, load_context, packet_defects

            context = context_path(run_root, worker)
            if context.exists() or "derived_context" in packet:
                defects = packet_defects(packet, load_context(context))
                if defects:
                    raise ValueError("; ".join(defects))
        accepted = accept_packet(packet, worker, source_dir=source_dir, adapters=ADAPTERS)
        for key, expected in revisions.items():
            if accepted.get(key) != expected:
                raise RuntimeError(f"accepted packet {worker} {key} drift")
        packets[worker] = accepted
    return packets


def _publish_sources(field_root, source_files, manifest_rows, source_root):
    rows = bind_manifest(source_files, manifest_rows, ADAPTERS, source_root=source_root)
    preserve_original_bytes(field_root)
    snapshots = field_root / "evidence/snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    for source in source_files:
        copy_source_snapshot(source, snapshots, source_root=source_root)
    return [
        dict(row, snapshot="evidence/snapshots/" + snapshot_relative(source, source_root).as_posix())
        for source, row in zip(source_files, rows)
    ]


def _source_name(source, source_root):
    return source.relative_to(source_root).as_posix() if source_root else source.name


def _bundle_note(field_root, source, source_root, name):
    key = snapshot_relative(source, source_root).parent.name
    path = field_root / "wiki" / "assets" / key / "bundle.json"
    if not path.is_file():
        return None
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("key") != key:
        raise RuntimeError(f"published native bundle identity drift: {name}")
    if (bundle.get("source") or {}).get("source_file") != name:
        raise RuntimeError(f"published native source identity drift: {name}")
    return f"wiki/summaries/{pathlib.Path(bundle['input_path']).stem}.md"


def _published_notes(field_root, source_files, source_root, notes, sources):
    result = dict(notes)
    for source in source_files:
        name = _source_name(source, source_root)
        source_id = sources.get(name)
        if not source_id:
            continue
        note = _bundle_note(field_root, source, source_root, name)
        if note:
            result[source_id] = note
    return result


def prepare_inputs(
    field_root,
    run_root,
    source_dir,
    source_files,
    manifest_rows,
    worker_ids,
    notes,
    sources,
    reading_plan,
    acceptance,
):
    source_root = source_dir if reading_plan else None
    packets = _accepted_packets(run_root, worker_ids, source_dir, reading_plan, acceptance)
    source_rows = _publish_sources(field_root, source_files, manifest_rows, source_root)
    if reading_plan:
        write_json(field_root / "evidence/reading-plan.json", reading_plan.data)
    published_notes = _published_notes(field_root, source_files, source_root, notes, sources)
    return packets, source_rows, published_notes
