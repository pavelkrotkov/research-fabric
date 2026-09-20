"""Atomic run terminal state and proposed-commit finalization (ADR-002)."""

import hashlib
import json
import logging
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


def commit_publication(kb, message, *, compiled=None, reviewed=None):
    """Create and verify the isolated proposed commit without declaring a run ready."""
    assert_run_branch(kb)
    outputs = _publication_outputs(kb)
    _verify_reviewed_outputs(kb, outputs, reviewed)
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
    return {"branch": branch, "commit": head, "evidence": str(Path(kb) / "evidence"), "outputs": outputs}


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
    """Compatibility wrapper: validate provenance, commit, then record READY."""
    record = provenance()
    if not isinstance(record, dict) or not record:
        raise CompilationError("Missing run provenance")
    publication = commit_publication(kb, message, compiled=compiled, reviewed=reviewed)
    return record_ready(run_root, claims, record, publication)


def _publication_outputs(kb):
    """Snapshot every publication artifact, excluding private native state."""
    outputs = {
        str(path.relative_to(kb)): digest(path)
        for folder in ("wiki", "evidence", "raw")
        for path in (Path(kb) / folder).rglob("*")
        if path.is_file()
    }
    if (Path(kb) / ".gitattributes").is_file():
        outputs[".gitattributes"] = digest(Path(kb) / ".gitattributes")
    return outputs


def _verify_reviewed_outputs(kb, outputs, reviewed):
    if reviewed is not None and (
        reviewed["base_commit"] != _git(kb, "rev-parse", "HEAD") or reviewed["outputs"] != outputs
    ):
        raise CompilationError("Publication outputs changed after compiled-wiki review")


def _verify_compiled_outputs(outputs, compiled):
    """Required compiled pages must survive publication with their attested bytes."""
    paths = {"wiki/index.md"}
    for source in compiled["sources"]:
        paths.add(f"wiki/summaries/{source['summary']}.md")
        paths.update(f"wiki/{folder}/{slug}.md" for folder, _, slug in source["required"])
    for path in paths:
        if path not in outputs or outputs[path] != compiled["outputs"].get(path):
            raise CompilationError(f"Required compiled output changed or disappeared: {path}")


def _require_tracked(kb, outputs):
    """Ignored publication files cannot count as a clean proposed commit."""
    tracked = set(_git(kb, "ls-files", "-z").split("\0"))
    missing = outputs.keys() - tracked
    if missing:
        raise CompilationError(f"Publication outputs are ignored/untracked: {', '.join(sorted(missing))}")


def _verify_commit(kb, head, outputs):
    """Bind accepted bytes to actual Git blobs, including changes by hooks."""
    for path, expected in outputs.items():
        blob = subprocess.run(
            ["git", "-C", str(kb), "cat-file", "blob", f"{head}:{path}"], capture_output=True, check=True
        ).stdout
        if hashlib.sha256(blob).hexdigest() != expected:
            raise CompilationError(f"Proposed commit changed publication output: {path}")
