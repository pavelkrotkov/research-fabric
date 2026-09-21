"""Create and verify isolated proposed Git publication commits.

This module owns only Git-facing publication mechanics. It snapshots the public
candidate bytes, binds them to the compiled/reviewed candidate, stages the same
publication tree for every caller, verifies the resulting commit blobs, and
requires a clean worktree. It never records terminal run state or merges main.
"""

import hashlib
import subprocess
from pathlib import Path

from .compilation import CompilationError, _git, assert_run_branch, digest


def _publication_outputs(kb):
    """Snapshot publication artifacts while excluding private native state."""
    outputs = {
        str(path.relative_to(kb)): digest(path)
        for folder in ("wiki", "evidence", "raw")
        for path in (Path(kb) / folder).rglob("*")
        if path.is_file()
    }
    attributes = Path(kb) / ".gitattributes"
    if attributes.is_file():
        outputs[".gitattributes"] = digest(attributes)
    return outputs


def _verify_reviewed_outputs(kb, outputs, reviewed):
    if reviewed is None:
        return
    if reviewed["base_commit"] != _git(kb, "rev-parse", "HEAD"):
        raise CompilationError("Publication outputs changed after compiled-wiki review")
    if reviewed["outputs"] != outputs:
        raise CompilationError("Publication outputs changed after compiled-wiki review")


def _compiled_paths(compiled):
    paths = {"wiki/index.md"}
    for source in compiled["sources"]:
        paths.add(f"wiki/summaries/{source['summary']}.md")
        paths.update(f"wiki/{folder}/{slug}.md" for folder, _, slug in source["required"])
    return paths


def _verify_compiled_outputs(outputs, compiled):
    """Required compiled pages must survive publication with attested bytes."""
    for path in _compiled_paths(compiled):
        if outputs.get(path) != compiled["outputs"].get(path):
            raise CompilationError(f"Required compiled output changed or disappeared: {path}")


def _require_tracked(kb, outputs):
    tracked = set(_git(kb, "ls-files", "-z").split("\0"))
    missing = outputs.keys() - tracked
    if missing:
        raise CompilationError(f"Publication outputs are ignored/untracked: {', '.join(sorted(missing))}")


def _verify_commit(kb, head, outputs):
    """Compare each accepted publication byte string to the proposed Git blob."""
    for path, expected in outputs.items():
        blob = subprocess.run(
            ["git", "-C", str(kb), "cat-file", "blob", f"{head}:{path}"],
            capture_output=True,
            check=True,
        ).stdout
        if hashlib.sha256(blob).hexdigest() != expected:
            raise CompilationError(f"Proposed commit changed publication output: {path}")


def commit_publication(kb, message, *, compiled=None, reviewed=None):
    """Stage, create and verify one isolated proposed publication commit."""
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
    if _git(kb, "status", "--porcelain"):
        raise CompilationError("Worktree dirty after commit; proposed commit is not accepted")
    return {
        "branch": _git(kb, "branch", "--show-current"),
        "commit": head,
        "evidence": str(Path(kb) / "evidence"),
        "outputs": outputs,
    }
