"""Shared post-compilation publication for production and offline rehearsal.

``publish_candidate`` is the concrete publication boundary. Callers provide an
accepted compile receipt, accepted packets, frozen sources, and an isolated
candidate. This module orchestrates existing identity/source projection, real
deterministic gates, candidate-bound review, and verified Git publication; it
never compiles, plans, merges main, or declares a run terminally ready.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

from ._publication_git import commit_publication
from ._publication_inputs import prepare_inputs
from ._publication_records import ledger_rows, materialize_evidence
from .compilation import CompilationError, write_json
from .review import advisory_record, audit_compiled_wiki


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


def _run_gates(engine_root, field_root, verification, claims, multi, alignment):
    """Run the same deterministic publication gates for every caller."""
    outcomes = [
        _gate(engine_root, field_root, verification, "provenance_validate.py", "provenance.txt"),
        _gate(engine_root, field_root, verification, "excerpt_grounding.py", "excerpt-grounding.txt"),
    ]
    if not multi or not any(claim.get("english_witness") for claim in claims):
        return outcomes
    path = pathlib.Path(alignment) if alignment else None
    if path is None or not path.is_file():
        raise RuntimeError("translation grounding lacks prepared alignment input")
    outcomes.append(
        _gate(engine_root, field_root, verification, "translation_grounding.py", "translation-grounding.txt", path)
    )
    return outcomes


def _advisory(review, callback):
    """Record semantic review availability without turning it into a gate."""
    scope = {"candidate_sha256": review["candidate_sha256"], "changed": review["changed"]}
    if callback is None:
        return advisory_record("", scope, error="offline rehearsal: advisory review unavailable")
    try:
        result = callback(review)
    except Exception as exc:
        return advisory_record("", scope, error=f"advisory review unavailable: {exc}")
    if isinstance(result, dict):
        return result
    return advisory_record("", scope, error="advisory review returned no record")


def _message(message, claims):
    return message(claims) if callable(message) else message


def _require_compiled(compiled):
    if not isinstance(compiled, dict) or compiled.get("state") != "COMPLETE":
        raise CompilationError("publication requires an accepted COMPLETE compilation report")
    if not compiled.get("sources") or not compiled.get("outputs"):
        raise CompilationError("accepted compilation report is incomplete")


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
    _require_compiled(compiled)
    field_root, run_root = pathlib.Path(field_root), pathlib.Path(run_root)
    engine_root, source_dir = pathlib.Path(engine_root), pathlib.Path(source_dir)
    source_files = list(map(pathlib.Path, source_files))
    verification = run_root / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    packets, source_rows, published_notes = prepare_inputs(
        field_root,
        run_root,
        source_dir,
        source_files,
        manifest_rows,
        worker_ids,
        notes,
        sources,
        reading_plan,
        {} if acceptance is None else acceptance,
    )
    claims = materialize_evidence(
        field_root,
        run_root,
        packets,
        published_notes,
        sources,
        source_rows,
        multi=multi,
        reading_plan=reading_plan,
    )
    gates = _run_gates(engine_root, field_root, verification, claims, multi, alignment)
    if before_review:
        before_review()
    review = audit_compiled_wiki(field_root, verification)
    advisory = _advisory(review, advisory)
    write_json(verification / "advisory-post-verifier.json", advisory)
    commit = commit_publication(field_root, _message(message, len(claims)), compiled=compiled, reviewed=review)
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
            "advisory_status": advisory["status"],
        },
    )
    return result


__all__ = ["ledger_rows", "publish_candidate"]
