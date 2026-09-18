"""Atomic run terminal state and proposed-commit finalization (ADR-002)."""

from contextlib import contextmanager
from pathlib import Path

from research_fabric.compilation import CompilationError, _git, assert_run_branch, write_json


@contextmanager
def run_lifecycle(run_root):
    """Clear stale success on entry and record every escaping failure atomically."""
    path = Path(run_root) / "run.json"
    write_json(path, {"run_id": Path(run_root).name, "state": "PLANNING"})
    try:
        yield
    except BaseException as exc:
        write_json(path, {"run_id": Path(run_root).name, "state": "FAILED", "failure": f"{type(exc).__name__}: {exc}"})
        raise


def finalize_run(kb, run_root, message, claims, provenance):
    """A proposed commit is ready only after provenance and clean-tree checks."""
    assert_run_branch(kb)
    record = provenance()
    if not isinstance(record, dict) or not record:
        raise CompilationError("Missing run provenance")
    _git(kb, "add", "-A")
    _git(kb, "diff", "--cached", "--check")
    _git(kb, "commit", "-m", message)
    head = _git(kb, "rev-parse", "HEAD")
    branch = _git(kb, "branch", "--show-current")
    if _git(kb, "status", "--porcelain"):
        raise CompilationError("Worktree dirty after commit; proposed commit is not accepted")
    result = {
        "run_id": Path(run_root).name,
        "state": "READY_FOR_REVIEW",
        "branch": branch,
        "commit": head,
        "claims": claims,
        "provenance": record,
    }
    write_json(Path(run_root) / "run.json", result)
    return result
