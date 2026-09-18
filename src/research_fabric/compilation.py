"""Accept complete native compilation in disposable candidates, never CLI success.

The executable child is intentional: OpenKB 0.4.5 has no strict result API, so
its version-specific instrumentation must never affect another run/thread.

This seam owns compile-stage completion only. Its receipt is not a publication
checkpoint: ledger materialization, deterministic audits and proposed-commit
finalization still follow. A workflow must keep its run non-terminal throughout.

Each attempt receives an independent byte-copy of the same baseline, including
native registry/database state. Native rollback is exercised inside that copy;
we never attempt to manufacture recovery by copying only the wiki pages back.
The original worktree is checked again before applying a complete candidate.

Retained candidates and their baseline are private run data, outside the Git
worktree. An interrupted application requires a fresh run branch; it cannot
resume by treating a partially copied registry as a completion receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Workflow scripts also invoke this file from a checkout, without packaging.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_fabric._openkb045 import POLICY, CompilationError, native_compile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def _files(root):
    _reject_journals(root)
    paths = sorted(root.rglob("*"))
    if any(p.is_symlink() for p in paths if ".git" not in p.relative_to(root).parts):
        raise CompilationError("Candidate contains symlinks; use a self-contained run worktree")
    return [p for p in paths if p.is_file() and ".git" not in p.relative_to(root).parts]


def _reject_journals(root):
    # Native journals contain absolute restore paths. Copying a pending journal
    # into a candidate could let native recovery reach the original worktree.
    if list((root / ".openkb" / "journal").glob("*.json")):
        raise CompilationError("Native mutation journal remains; recover it in its original KB before starting a run")


def _tree(root):
    return {str(p.relative_to(root)): digest(p) for p in _files(root)}


def _git(kb, *args):
    return subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True, text=True).stdout.strip()


def assert_run_branch(kb):
    if _git(kb, "branch", "--show-current") in ("", "main", "master"):
        raise CompilationError("Compilation/publication requires an isolated non-main branch")


def _identity(kb, sources, command):
    """Bind both policy modules and the execution environment to baseline bytes."""
    # Full baseline binds native registry, policy/config, existing pages and
    # assets. Command must use an explicit interpreter; child verifies versions.
    return {
        "policy": POLICY,
        "base_commit": _git(kb, "rev-parse", "HEAD"),
        "branch": _git(kb, "branch", "--show-current"),
        "baseline": _tree(kb),
        "sources": {str(p): digest(p) for p in sources},
        "command": list(command),
        "adapter_sha256": digest(__file__),
        "native_adapter_sha256": digest(Path(__file__).with_name("_openkb045.py")),
        "environment_sha256": hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest(),
        "executable_sha256": digest(shutil.which(command[0]) or command[0]),
        "entrypoint_sha256": digest(command[1]) if len(command) > 1 and Path(command[1]).is_file() else None,
    }


def _apply_candidate(candidate, kb, baseline):
    if _tree(kb) != baseline:
        raise CompilationError("Run worktree changed during compilation; candidate quarantined")
    # No custom rollback engine: failure/crash leaves a non-ready run. Resume
    # from the retained baseline, never accept an interrupted copy.
    current = _tree(candidate)
    for name in baseline.keys() - current.keys():
        (kb / name).unlink()
    for name, sha in current.items():
        if baseline.get(name) == sha:
            continue
        destination = kb / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.chmod(0o644)
        shutil.copy2(candidate / name, destination)


def compile_with_recovery(kb, sources, diagnostics, *, command=None, attempts=2):
    """Compile all inputs from an unchanged baseline; retain failed diagnostics.

    command is a controlled executable implementing --kb/--sources/--result.
    The default invokes this exact module under the current interpreter, which
    must have the qualified OpenKB dependencies installed.
    """
    kb, diagnostics = Path(kb).resolve(), Path(diagnostics).resolve()
    sources = tuple(Path(p).resolve() for p in sources)
    command = tuple(command or (sys.executable, str(Path(__file__).resolve())))
    identity, session, baseline = _prepare_attempts(kb, sources, diagnostics, command, attempts)
    for attempt in range(1, attempts + 1):
        if _identity(kb, sources, command) != identity:
            raise CompilationError("Input, policy or worktree identity changed; start a new compile attempt")
        candidate = session / f"candidate-{attempt}"
        shutil.copytree(baseline, candidate)
        result_file = session / f"result-{attempt}.json"
        argv = [*command, "--kb", str(candidate), "--result", str(result_file), "--sources", *map(str, sources)]
        try:
            with (session / f"attempt-{attempt}.log").open("w") as log:
                result = subprocess.run(
                    argv, cwd=candidate, stdout=log, stderr=subprocess.STDOUT, check=False, timeout=1800
                )
            report = json.loads(result_file.read_text())
            _validate_result(candidate, report, sources, result.returncode)
            if _identity(kb, sources, command) != identity:
                raise CompilationError("Input/policy changed while compiler was running")
            _apply_candidate(candidate, kb, identity["baseline"])
            write_json(session / "completed.json", {"report": report, "outputs": _tree(kb)})
            return report
        except (OSError, ValueError, KeyError, CompilationError, subprocess.TimeoutExpired) as exc:
            write_json(session / f"failure-{attempt}.json", {"state": "FAILED", "reason": str(exc)})
    raise CompilationError(f"Compilation failed after {attempts} attempt(s); diagnostics and baseline: {session}")


def _prepare_attempts(kb, sources, diagnostics, command, attempts):
    if not sources or not 1 <= attempts <= 3:
        raise CompilationError("Supply sources and 1–3 bounded attempts")
    if diagnostics.is_relative_to(kb):
        raise CompilationError("Compile diagnostics must be outside the candidate worktree")
    assert_run_branch(kb)
    identity = _identity(kb, sources, command)
    diagnostics.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="compile-", dir=diagnostics))
    write_json(session / "identity.json", identity)
    baseline = session / "baseline"
    shutil.copytree(kb, baseline, ignore=shutil.ignore_patterns(".git"))
    return identity, session, baseline


def normalize_generated_log(kb):
    """Remove native log EOF padding before hashing or reviewing a candidate.

    OpenKB append_log ends entries with a blank line, which git diff --check
    correctly rejects. This touches only its generated operations log, never
    page bodies, immutable sources, snapshots, or intentional Markdown spaces.
    """
    log = Path(kb) / "wiki" / "log.md"
    if log.is_file():
        log.write_text(log.read_text().rstrip("\n") + "\n")


def _validate_result(candidate, report, sources, returncode):
    _check_completion_header(report, returncode)
    if {row["sha256"] for row in report["sources"]} != {digest(p) for p in sources}:
        raise CompilationError("Compiler source outcomes do not match inputs")
    if not report.get("outputs") or report["outputs"] != _tree(candidate):
        raise CompilationError("Native applied-output attestation differs from candidate")
    for row in report["sources"]:
        _validate_source_outputs(candidate, row)


def _check_completion_header(report, returncode):
    if returncode or report.get("policy") != POLICY or report.get("state") != "COMPLETE":
        raise CompilationError("Compiler exited unsuccessfully or returned no complete outcome")
    if report.get("versions") != {"openkb": "0.4.5", "litellm": "1.87.2"}:
        raise CompilationError("Compiler toolchain differs from qualified versions")


def _validate_source_outputs(candidate, row):
    for folder, action, slug in row["required"]:
        if folder not in ("concepts", "entities") or action not in ("create", "update", "related"):
            raise CompilationError("Unknown required operation")
        path = candidate / "wiki" / folder / f"{slug}.md"
        if not path.resolve().is_relative_to(candidate) or not path.is_file():
            raise CompilationError("Required output absent or outside candidate")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--sources", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    try:
        # Native errors can echo provider bodies/source excerpts; retain only
        # normalized outcomes, never raw native logs in persisted diagnostics.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            report = native_compile(args.kb, args.sources)
        normalize_generated_log(args.kb)
        report.update(state="COMPLETE", outputs=_tree(args.kb))
        write_json(args.result, report)
    except Exception as exc:
        # Class + actionable adapter errors only; provider exception bodies may
        # contain credentials/source text, so do not serialize arbitrary errors.
        reason = str(exc) if isinstance(exc, CompilationError) else type(exc).__name__
        write_json(args.result, {"state": "FAILED", "reason": reason})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
