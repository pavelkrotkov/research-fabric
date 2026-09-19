"""Production seam and terminal-state regressions with disposable Git worktrees."""

import json
import subprocess
import sys

import pytest

from research_fabric.compilation import (
    CompilationError,
    compile_with_recovery,
)
from research_fabric.run_state import finalize_run, run_lifecycle


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "baseline").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    git(root, "checkout", "-b", "agent/test")
    return root


@pytest.fixture
def executable(tmp_path):
    script = tmp_path / "controlled.py"
    script.write_text("""
import argparse, hashlib, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--kb', type=Path)
p.add_argument('--result', type=Path)
p.add_argument('--sources', nargs='+', type=Path)
p.add_argument('--mode')
p.add_argument('--execution-root')
p.add_argument('--profile-index')
a = p.parse_args()
page = a.kb / 'wiki/concepts/one.md'
page.parent.mkdir(parents=True, exist_ok=True)
page.write_text('Generated body')
registry = a.kb / '.openkb/hashes.json'
registry.parent.mkdir(exist_ok=True)
registry.write_text('{"partial": true}')
if a.mode == 'timeout':
    raise TimeoutError('controlled timeout')
if a.mode == 'missing':
    raise SystemExit(0)
outputs = {str(p.relative_to(a.kb)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in a.kb.rglob('*') if p.is_file()}
report = {'state': 'COMPLETE', 'policy': 'native-045-complete-v1',
          'versions': {'openkb': '0.4.5', 'litellm': '1.87.2'}, 'outputs': outputs,
          'sources': [{'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                       'required': [['concepts', 'create', 'one']]} for p in a.sources]}
if a.mode == 'partial':
    report['sources'][0]['required'].append(['concepts', 'update', 'two'])
if a.mode == 'write-failure':
    page.write_text('Truncated write after response')
a.result.write_text(json.dumps(report))
""")
    return script


@pytest.mark.parametrize("mode", ["partial", "timeout", "missing", "write-failure"])
def test_production_seam_quarantines_failures(repo, tmp_path, executable, mode):
    src = tmp_path / "source.md"
    src.write_text("source")
    run = tmp_path / "run"
    main = git(repo, "rev-parse", "main")
    with pytest.raises(CompilationError), run_lifecycle(run):
        compile_with_recovery(
            repo, [src], run / "compile", attempts=2, command=[sys.executable, str(executable), "--mode", mode]
        )
    assert json.loads((run / "run.json").read_text())["state"] == "FAILED"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == main
    assert len(list((run / "compile").glob("*/failure-*.json"))) == 2
    assert not list((run / "compile").glob("*/completed.json"))


def test_success_only_after_commit_and_clean_tree(repo, tmp_path, executable):
    src = tmp_path / "source.md"
    src.write_text("source")
    run = tmp_path / "run"
    main = git(repo, "rev-parse", "main")

    def provenance():
        assert json.loads((run / "run.json").read_text())["state"] != "READY_FOR_REVIEW"
        return {"engine_sha": "fixture"}

    with run_lifecycle(run):
        compile_with_recovery(
            repo, [src], run / "compile", command=[sys.executable, str(executable), "--mode", "success"]
        )
        result = finalize_run(repo, run, "test proposal", 1, provenance)
    assert result["state"] == "READY_FOR_REVIEW"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "main") == main
    assert result["commit"] != main


@pytest.mark.parametrize("failure", ["commit", "check", "dirty", "provenance", "unexpected"])
def test_publication_failure_never_ready(repo, tmp_path, failure):
    run = tmp_path / "run"
    (repo / "change").write_text("trailing space \n" if failure == "check" else "clean\n")
    if failure in ("commit", "dirty"):
        hook = repo / ".git/hooks" / ("pre-commit" if failure == "commit" else "post-commit")
        hook.write_text("#!/bin/sh\n" + ("exit 1\n" if failure == "commit" else "echo dirty > unexpected\n"))
        hook.chmod(0o755)

    def provenance():
        assert json.loads((run / "run.json").read_text())["state"] == "PLANNING"
        if failure == "provenance":
            raise RuntimeError("provenance unavailable")
        return {"engine_sha": "fixture"}

    with pytest.raises((RuntimeError, ValueError, subprocess.CalledProcessError)), run_lifecycle(run):
        if failure == "unexpected":
            raise ValueError("unexpected workflow exception")
        finalize_run(repo, run, "test proposal", 1, provenance)
    result = json.loads((run / "run.json").read_text())
    assert result["state"] == "FAILED"
    assert result["failure"]


def test_main_is_never_modified(repo, tmp_path, executable):
    git(repo, "checkout", "main")
    src = tmp_path / "source"
    src.write_text("x")
    with pytest.raises(CompilationError, match="isolated"):
        compile_with_recovery(
            repo, [src], tmp_path / "run", command=[sys.executable, str(executable), "--mode", "success"]
        )
    assert git(repo, "status", "--porcelain") == ""


def test_pending_native_journal_cannot_escape_candidate(repo, tmp_path, executable):
    journal = repo / ".openkb/journal/pending.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"kb_dir": str(repo), "status": "active"}))
    source = tmp_path / "source"
    source.write_text("input")
    with pytest.raises(CompilationError, match="journal remains"):
        compile_with_recovery(
            repo, [source], tmp_path / "run", command=[sys.executable, str(executable), "--mode", "success"]
        )
    assert journal.exists()
    assert (repo / "baseline").read_text() == "original\n"


def test_input_drift_is_rejected_before_candidate_application(repo, tmp_path, executable):
    source = tmp_path / "source"
    source.write_text("original input")
    # Controlled executable changes input after attesting its original digest.
    with executable.open("a") as script:
        script.write("\na.sources[0].write_text('changed input')\n")
    with pytest.raises(CompilationError):
        compile_with_recovery(
            repo, [source], tmp_path / "run", attempts=1, command=[sys.executable, str(executable), "--mode", "success"]
        )
    assert git(repo, "status", "--porcelain") == ""


def test_clean_commit_hook_rewrite_is_not_accepted(repo, tmp_path):
    page = repo / "wiki/page.md"
    page.parent.mkdir()
    page.write_text("gated bytes\n")
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'changed after gates\\n' > wiki/page.md\ngit add wiki/page.md\n")
    hook.chmod(0o755)
    run = tmp_path / "run"
    with pytest.raises(CompilationError, match="changed publication"), run_lifecycle(run):
        finalize_run(repo, run, "proposal", 1, lambda: {"fixture": True})
    assert git(repo, "status", "--porcelain") == ""
    assert json.loads((run / "run.json").read_text())["state"] == "FAILED"


def test_compile_profile_switch_during_candidate_never_accepts_stale_result(repo, tmp_path, executable, monkeypatch):
    from research_fabric.execution import configure, resolve

    source = tmp_path / "source.md"
    source.write_text("immutable source")
    run = tmp_path / "run"
    configure(run, resolve(native_model="openai/B", environ={}))
    original = subprocess.run

    def switch_after_child(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if str(executable) in command:
            configure(run, resolve(native_model="openai/C", environ={}))
        return result

    monkeypatch.setattr(subprocess, "run", switch_after_child)
    with pytest.raises(CompilationError):
        compile_with_recovery(
            repo, [source], run / "compile", command=[sys.executable, str(executable)], execution_root=run, attempts=1
        )
    assert not (repo / "wiki/concepts/one.md").exists()
    assert git(repo, "status", "--porcelain") == ""
    failure = json.loads(next((run / "compile").glob("*/failure-1.json")).read_text())
    assert "Input/policy changed" in failure["reason"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unwritable"])
def test_failure_recording_preserves_original_error_and_damaged_state(tmp_path, monkeypatch, caplog, damage):
    from research_fabric import run_state

    run = tmp_path / "run"
    original = RuntimeError("original worker failure")
    with pytest.raises(RuntimeError) as caught, run_state.run_lifecycle(run):
        path = run / "run.json"
        run_state.set_state(run, "RESEARCHING")
        if damage == "missing":
            path.unlink()
        elif damage == "corrupt":
            path.write_text("broken JSON")
        else:

            def reject_write(*args):
                raise OSError("state storage unavailable")

            monkeypatch.setattr(run_state, "write_json", reject_write)
        before = path.read_bytes() if path.exists() else None
        raise original
    assert caught.value is original
    assert (path.read_bytes() if path.exists() else None) == before
    assert f"Cannot record FAILED at {path}" in caplog.text
    assert "original worker failure" in caplog.text
    assert "UNKNOWN" not in caplog.text


def test_initial_state_write_failure_does_not_enter_run(tmp_path, monkeypatch):
    from research_fabric import run_state

    def reject_write(*args):
        raise OSError("initial state write failed")

    monkeypatch.setattr(run_state, "write_json", reject_write)
    with pytest.raises(OSError, match="initial state write failed"), run_state.run_lifecycle(tmp_path):
        pytest.fail("execution began without the initial state record")
