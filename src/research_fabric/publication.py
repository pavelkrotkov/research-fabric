"""Shared post-compilation publication for production and offline rehearsal."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from ._publication_records import ledger_rows, materialize_evidence
from ._source_adapter import ADAPTERS
from .claims import accept_packet
from .compilation import CompilationError, write_json
from .review import advisory_record, audit_compiled_wiki
from .run_state import commit_publication
from .sources import bind_manifest, copy_source_snapshot, preserve_original_bytes, snapshot_relative


def _accepted_packets(run_root, worker_ids, source_dir, reading_plan, acceptance):
    packets = {}
    for worker in worker_ids:
        path = run_root / "evidence" / f"worker-{worker}.json"
        packet = json.loads(path.read_text(encoding="utf-8"))
        revisions = {key: packet.get(key) for key in ("packet_revision", "packet_state_revision", "source_revision")}
        if reading_plan:
            reading_plan.validate_packet(packet, worker, acceptance)
        accepted = accept_packet(packet, worker, source_dir=source_dir, adapters=ADAPTERS)
        for key, expected in revisions.items():
            if expected is not None and accepted.get(key) != expected:
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


def _published_notes(field_root, source_files, source_root, notes, sources):
    result = dict(notes)
    for source in source_files:
        name = source.relative_to(source_root).as_posix() if source_root else source.name
        source_id = sources.get(name)
        if not source_id:
            continue
        key = snapshot_relative(source, source_root).parent.name
        bundle_path = field_root / "wiki" / "assets" / key / "bundle.json"
        if not bundle_path.is_file():
            continue
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        if bundle.get("key") != key or (bundle.get("source") or {}).get("source_file") != name:
            raise RuntimeError(f"published native source identity drift: {name}")
        result[source_id] = f"wiki/summaries/{pathlib.Path(bundle['input_path']).stem}.md"
    return result


def _gate(engine_root, field_root, verification, script, artifact, *extra):
    result = subprocess.run(
        [sys.executable, str(engine_root / "bin" / script), str(field_root), *map(str, extra)],
        text=True,
        capture_output=True,
    )
    path = verification / artifact
    path.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{script} failed; preserved diagnostics: {path}")
    return {"script": script, "artifact": artifact, "returncode": result.returncode}


def _gates(engine_root, field_root, verification, claims, multi, alignment):
    outcomes = [
        _gate(engine_root, field_root, verification, "provenance_validate.py", "provenance.txt"),
        _gate(engine_root, field_root, verification, "excerpt_grounding.py", "excerpt-grounding.txt"),
    ]
    if multi and any(claim.get("english_witness") for claim in claims):
        alignment = pathlib.Path(alignment) if alignment else None
        if alignment is None or not alignment.is_file():
            raise RuntimeError("translation grounding lacks prepared alignment input")
        outcomes.append(
            _gate(
                engine_root,
                field_root,
                verification,
                "translation_grounding.py",
                "translation-grounding.txt",
                alignment,
            )
        )
    return outcomes


def _advisory(review, callback):
    scope = {"candidate_sha256": review["candidate_sha256"], "changed": review["changed"]}
    if callback is None:
        return advisory_record("", scope, error="offline rehearsal: advisory review unavailable")
    try:
        record = callback(review)
    except Exception as exc:
        return advisory_record("", scope, error=f"advisory review unavailable: {exc}")
    if isinstance(record, dict):\n        return record\n    return advisory_record("", scope, error="advisory review returned no record")


def publish_candidate(
    *,
    field_root,
    run_root,
    engine_root,
    source_dir,
    source_files,
    manifest_rows,
    worker_ids,
    notes,
    sources,
    compiled,
    message,
    multi=False,
    reading_plan=None,
    acceptance=None,
    alignment=None,
    advisory=None,
    before_review=None,
):
    """Materialize, gate, review and commit one prepared compiled candidate."""
    if not compiled:
        raise CompilationError("publication requires an accepted compilation report")
    field_root, run_root = pathlib.Path(field_root), pathlib.Path(run_root)
    engine_root, source_dir = pathlib.Path(engine_root), pathlib.Path(source_dir)
    source_files = [pathlib.Path(path) for path in source_files]
    verification = run_root / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    source_root = source_dir if reading_plan else None
    packets = _accepted_packets(run_root, worker_ids, source_dir, reading_plan, acceptance or {})
    source_rows = _publish_sources(field_root, source_files, manifest_rows, source_root)
    if reading_plan:
        write_json(field_root / "evidence/reading-plan.json", reading_plan.data)
    claims = materialize_evidence(
        field_root,
        run_root,
        packets,
        _published_notes(field_root, source_files, source_root, notes, sources),
        sources,
        source_rows,
        multi=multi,
        reading_plan=reading_plan,
    )
    gates = _gates(engine_root, field_root, verification, claims, multi, alignment)
    if before_review:
        before_review()
    review = audit_compiled_wiki(field_root, verification)
    advisory_result = _advisory(review, advisory)
    write_json(verification / "advisory-post-verifier.json", advisory_result)
    commit = commit_publication(
        field_root,
        message(len(claims)) if callable(message) else message,
        compiled=compiled,
        reviewed=review,
    )
    result = {**commit, "claims": claims, "gates": gates, "review": review, "source_rows": source_rows}
    write_json(
        verification / "publication-result.json",
        {
            "commit": commit["commit"],
            "claims": len(claims),
            "gates": gates,
            "outputs": sorted(commit["outputs"]),
            "changed": review["changed"],
            "candidate_sha256": review["candidate_sha256"],
            "advisory_status": advisory_result["status"],
        },
    )
    return result


__all__ = ["ledger_rows", "publish_candidate"]
