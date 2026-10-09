"""Atomic run terminal state (ADR-002); Git publication lives with the publisher.

The engine owns terminal run state. Publication commit mechanics are re-exported
for compatibility with older callers, but their implementation lives in the
publication component.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path

from . import _publication_git
from .compilation import CompilationError, write_json

commit_publication = _publication_git.commit_publication
_publication_outputs = _publication_git._publication_outputs
_verify_compiled_outputs = _publication_git._verify_compiled_outputs
_verify_reviewed_outputs = _publication_git._verify_reviewed_outputs


def set_state(run_root, state):
    path = Path(run_root) / "run.json"
    record = json.loads(path.read_text())
    record["state"] = state
    write_json(path, record)


@contextmanager
def run_lifecycle(run_root):
    """Clear stale success on entry and record every escaping failure atomically."""
    path = Path(run_root) / "run.json"
    write_json(path, {"run_id": Path(run_root).name, "state": "PLANNING"})
    try:
        yield
    except BaseException as exc:
        try:
            record = json.loads(path.read_text())
            record.update(
                state="FAILED",
                failed_stage=record["state"],
                failure=f"{type(exc).__name__}: {exc}",
                artifacts=str(Path(run_root).resolve()),
            )
            write_json(path, record)
        except Exception:
            logging.exception("Cannot record FAILED at %s; preserving original failure: %s", path, exc)
        raise


def record_ready(run_root, claims, provenance, publication):
    """The engine alone turns a validated publication outcome into terminal READY."""
    if not isinstance(provenance, dict) or not provenance:
        raise CompilationError("Missing run provenance")
    result = {
        "run_id": Path(run_root).name,
        "state": "READY_FOR_REVIEW",
        "branch": publication["branch"],
        "commit": publication["commit"],
        "claims": claims,
        "evidence": publication["evidence"],
        "provenance": provenance,
    }
    write_json(Path(run_root) / "run.json", result)
    return result


def finalize_run(kb, run_root, message, claims, provenance, *, compiled=None, reviewed=None):
    """Compatibility wrapper for callers that still combine commit and READY."""
    record = provenance()
    if not isinstance(record, dict) or not record:
        raise CompilationError("Missing run provenance")
    publication = commit_publication(kb, message, compiled=compiled, reviewed=reviewed)
    return record_ready(run_root, claims, record, publication)
