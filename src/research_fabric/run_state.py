"""Atomic run terminal state and proposed-commit finalization (ADR-002)."""

import hashlib
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

from research_fabric.compilation import CompilationError, _git, assert_run_branch, digest, write_json


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
        record = json.loads(path.read_text())
        record.update(
            state="FAILED",
            failed_stage=record["state"],
            failure=f"{type(exc).__name__}: {exc}",
            artifacts=str(Path(run_root).resolve()),
        )
        write_json(path, record)
        raise


def finalize_run(kb, run_root, message, claims, provenance, *, compiled=None):
    """A proposed commit is ready only after provenance and clean-tree checks."""
    assert_run_branch(kb)
    record = provenance()
    if not isinstance(record, dict) or not record:
        raise CompilationError("Missing run provenance")
    outputs = _publication_outputs(kb)
    if compiled is not None:
        _verify_compiled_outputs(outputs, compiled)
    _git(kb, "add", "-A")
    _require_tracked(kb, outputs)
    _git(kb, "diff", "--cached", "--check")
    _git(kb, "commit", "-m", message)
    head = _git(kb, "rev-parse", "HEAD")
    _verify_commit(kb, head, outputs)
    branch = _git(kb, "branch", "--show-current")
    if _git(kb, "status", "--porcelain"):
        raise CompilationError("Worktree dirty after commit; proposed commit is not accepted")
    result = {
        "run_id": Path(run_root).name,
        "state": "READY_FOR_REVIEW",
        "branch": branch,
        "commit": head,
        "claims": claims,
        "evidence": str(Path(kb) / "evidence"),
        "provenance": record,
    }
    write_json(Path(run_root) / "run.json", result)
    return result


def _publication_outputs(kb):
    """Snapshot every publication artifact, excluding private native state."""
    return {
        str(path.relative_to(kb)): digest(path)
        for folder in ("wiki", "evidence")
        for path in (Path(kb) / folder).rglob("*")
        if path.is_file()
    }


def _verify_compiled_outputs(outputs, compiled):
    """Compilation pages must survive later phases with their attested bytes.

    The native operations log and lint reports intentionally change after
    compilation. Required summary/concept/entity pages and index do not.
    Ledger/snapshot output is included separately in the final publication
    snapshot, so this does not create a second compiled-output audit.
    """
    paths = {"wiki/index.md"}
    for source in compiled["sources"]:
        paths.add(f"wiki/summaries/{source['summary']}.md")
        paths.update(f"wiki/{folder}/{slug}.md" for folder, _, slug in source["required"])
    for path in paths:
        if path not in outputs or outputs[path] != compiled["outputs"][path]:
            raise CompilationError(f"Required compiled output changed or disappeared: {path}")


def _require_tracked(kb, outputs):
    """Ignored publication files cannot count as a clean proposed commit."""
    tracked = set(_git(kb, "ls-files", "-z").split("\0"))
    missing = outputs.keys() - tracked
    if missing:
        raise CompilationError(f"Publication outputs are ignored/untracked: {', '.join(sorted(missing))}")


def _verify_commit(kb, head, outputs):
    """Bind accepted bytes to actual Git blobs, including changes by hooks.

    A clean worktree is insufficient if a commit hook rewrote both the index
    and working copy. Read the proposed commit itself and compare it to the
    snapshot taken after gates and before staging. Never force-add ignored
    native/auth state, and leave rejected proposed commits on the run branch.
    """
    for path, expected in outputs.items():
        blob = subprocess.run(
            ["git", "-C", str(kb), "cat-file", "blob", f"{head}:{path}"], capture_output=True, check=True
        ).stdout
        if hashlib.sha256(blob).hexdigest() != expected:
            raise CompilationError(f"Proposed commit changed publication output: {path}")
