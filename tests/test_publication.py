import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_fabric.claims import accept_packet, atomic_write_json
from research_fabric.compilation import CompilationError
from research_fabric.publication import publish_candidate
from research_fabric.run_state import _publication_outputs

ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _case(tmp_path, excerpt="Athena spoke to Telemachus.", source_name="source.html"):
    repo, run, sources = tmp_path / "kb", tmp_path / "run", tmp_path / "sources"
    (repo / "wiki").mkdir(parents=True)
    (repo / "wiki/index.md").write_text("# Index\n")
    for args in (
        ("init", "-b", "agent/publication"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-m", "baseline"),
    ):
        _git(repo, *args)
    sources.mkdir()
    source = sources / source_name
    source.write_text("<p>Athena spoke to Telemachus.</p>\n")
    packet = accept_packet(
        {
            "parsed": {
                "claims": [
                    {
                        "claim": "Athena speaks",
                        "source_file": source.name,
                        "locator": "paragraph 1",
                        "excerpt": excerpt,
                        "stance": "supports",
                        "confidence": 0.9,
                    }
                ]
            }
        },
        "book-1",
        source_dir=sources,
    )
    (run / "evidence").mkdir(parents=True)
    atomic_write_json(run / "evidence/worker-book-1.json", packet)
    summary = repo / "wiki/summaries/source.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("# Source\n\nPrepared compiled page.\n")
    manifest = [
        {
            "source_id": "s-1",
            "snapshot": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "url": "https://example.invalid/source",
            "title": "Synthetic source",
            "retrieved_at": "2026-01-01",
            "content_type": "text/html",
        }
    ]
    compiled = {
        "state": "COMPLETE",
        "sources": [{"summary": "source", "required": []}],
        "outputs": _publication_outputs(repo),
    }
    return {
        "field_root": repo,
        "run_root": run,
        "engine_root": ROOT,
        "source_dir": sources,
        "source_files": [source],
        "manifest_rows": manifest,
        "worker_ids": ["book-1"],
        "notes": {"s-1": "wiki/summaries/source.md"},
        "sources": {source.name: "s-1"},
        "compiled": compiled,
        "message": lambda claims: f"test publication ({claims} claims)",
    }


def _publish(case):
    return publish_candidate(**case)


def test_shared_publication_runs_real_gates_and_verified_commit(tmp_path):
    case = _case(tmp_path)
    result = _publish(case)
    assert [row["script"] for row in result["gates"]] == ["provenance_validate.py", "excerpt_grounding.py"]
    assert result["claims"][0]["claim_id"] == "c-book-1-1"
    assert result["review"]["mechanical"]["ok"]
    advisory = json.loads((case["run_root"] / "verification/advisory-post-verifier.json").read_text())
    assert advisory["status"] == "unavailable"
    assert result["commit"] == _git(case["field_root"], "rev-parse", "HEAD")
    assert _git(case["field_root"], "status", "--porcelain") == ""
    assert set(result["outputs"]) == set(_publication_outputs(case["field_root"]))


@pytest.mark.parametrize(
    "fault,match",
    [
        ("manifest", "sha256 mismatch"),
        ("unmapped", "could not be mapped"),
        ("missing_note", "could not be mapped"),
    ],
)
def test_shared_publication_rejects_manifest_and_mapping_failures(tmp_path, fault, match):
    case = _case(tmp_path)
    if fault == "manifest":
        case["manifest_rows"][0]["sha256"] = "0" * 64
    elif fault == "unmapped":
        case["sources"] = {}
    else:
        case["notes"] = {}
    with pytest.raises(RuntimeError, match=match):
        _publish(case)


def test_shared_publication_requires_accepted_packet_metadata(tmp_path):
    case = _case(tmp_path)
    path = case["run_root"] / "evidence/worker-book-1.json"
    packet = json.loads(path.read_text())
    for key in ("packet_revision", "packet_state_revision", "source_revision"):
        packet.pop(key, None)
    atomic_write_json(path, packet)
    with pytest.raises(RuntimeError, match="accepted packet metadata"):
        _publish(case)


def test_shared_publication_rejects_stale_evidence_revision(tmp_path):
    case = _case(tmp_path)
    source = case["source_files"][0]
    source.write_text(source.read_text() + "<p>new bytes</p>\n")
    case["manifest_rows"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="claim source binding changed"):
        _publish(case)


def test_shared_publication_rejects_real_grounding_failure(tmp_path):
    case = _case(tmp_path, excerpt="Invented quotation")
    with pytest.raises(RuntimeError, match="excerpt_grounding.py failed"):
        _publish(case)


def test_shared_publication_rejects_stale_or_missing_compiled_output(tmp_path):
    stale = _case(tmp_path / "stale")
    (stale["field_root"] / "wiki/summaries/source.md").write_text("# Source\n\nchanged after compile\n")
    with pytest.raises(CompilationError, match="Required compiled output changed"):
        _publish(stale)

    missing = _case(tmp_path / "missing")
    missing["notes"] = {"s-1": "wiki/index.md"}
    (missing["field_root"] / "wiki/summaries/source.md").unlink()
    with pytest.raises(CompilationError, match="Required compiled output changed or disappeared"):
        _publish(missing)


@pytest.mark.parametrize("hook,match", [("pre-commit", "returned non-zero"), ("post-commit", "Worktree dirty")])
def test_shared_publication_rejects_commit_failure_or_dirty_tree(tmp_path, hook, match):
    case = _case(tmp_path)
    path = case["field_root"] / ".git/hooks" / hook
    path.write_text("#!/bin/sh\n" + ("exit 1\n" if hook == "pre-commit" else "touch leftover.tmp\n"))
    path.chmod(0o755)
    with pytest.raises((CompilationError, subprocess.CalledProcessError), match=match):
        _publish(case)


def test_shared_empty_diff_policy_is_fail_closed(tmp_path):
    case = _case(tmp_path)
    _publish(case)
    with pytest.raises(CompilationError, match="candidate diff is empty"):
        _publish(case)


def test_publication_root_aliases_preserve_identity_and_reject_escape(tmp_path):
    case = _case(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "real", target_is_directory=True)
    expected = _publish(_case(tmp_path / "expected"))
    case.update(source_dir=alias / "sources", run_root=alias / "run", field_root=alias / "kb")
    actual = _publish(case)
    assert actual["claims"] == expected["claims"]
    assert actual["source_rows"] == expected["source_rows"]
    assert actual["gates"] == expected["gates"]
    source = case["source_files"][0]
    outside = tmp_path / "outside.html"
    source.rename(outside)
    source.symlink_to(outside)
    with pytest.raises(ValueError, match="source escapes allowed root"):
        _publish(case)


@pytest.mark.parametrize(
    "fault", [None, "manifest", "escape", "missing_branch", "main", "no_branch", "pre-commit", "post-commit"]
)
def test_rehearsal_subprocess_is_portable_and_preserves_inputs(tmp_path, fault):
    case = _case(tmp_path / "input", source_name="source1.html")
    repo, run = case["field_root"], case["run_root"]
    _git(repo, "branch", "-m", "main")  # The only input branch is protected.
    shutil.copytree(case["source_dir"], run / "sources")
    if fault == "manifest":
        case["manifest_rows"][0]["sha256"] = "0" * 64
    if fault == "escape":
        source = run / "sources/source1.html"
        source.unlink()
        source.symlink_to(case["source_files"][0])
    (run / "source-manifest.jsonl").write_text(json.dumps(case["manifest_rows"][0]) + "\n")
    atomic_write_json(run / "verification/compile/accepted/completed.json", {"report": case["compiled"]})
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "synthetic.yaml").write_text(
        "corpus_dir: unused\nmanifest_path: unused\nsnapshot_pattern: 'source(1)\\.html'\n"
        "source_id_template: s-1\nnote_template: wiki/summaries/source.md\nbook_label_template: source1.html\n"
    )
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "input", target_is_directory=True)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(temporary, target_is_directory=True)

    def snapshot():
        return {
            str(path.relative_to(tmp_path / "input")): path.read_bytes()
            for path in (tmp_path / "input").rglob("*")
            if path.is_file()
        }

    before = snapshot()
    refs = _git(repo, "show-ref")
    branch = [] if fault == "no_branch" else ["--base", "missing" if fault == "missing_branch" else "main"]
    if fault == "main":
        branch = ["--branch", "main"]
    env = {**os.environ, "TMPDIR": str(temp_alias)}
    if fault in ("pre-commit", "post-commit"):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / fault
        hook.write_text("#!/bin/sh\n" + ("exit 1\n" if fault == "pre-commit" else "touch leftover.tmp\n"))
        hook.chmod(0o755)
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(hooks))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/dryrun_publication.py"),
            str(alias / "run"),
            str(alias / "kb"),
            "--projects-dir",
            str(projects),
            "--project",
            "synthetic",
            *branch,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (fault is None), result.stdout + result.stderr
    assert ("RESULT: OK" in result.stdout) == (fault is None)
    assert snapshot() == before
    assert _git(repo, "show-ref") == refs
    assert _git(repo, "branch", "--show-current") == "main"
    if fault in ("escape", "missing_branch", "main", "no_branch"):
        assert not list(temporary.iterdir())
