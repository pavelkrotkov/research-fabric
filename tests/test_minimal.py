"""Minimal opt-in acceptance: real native compilation, Git, rollback and accounting."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("openkb")
from openkb.schema import AGENTS_MD, INDEX_SEED
from openkb.state import HashRegistry

from research_fabric import minimal
from research_fabric._minimal_transport import check_budget
from research_fabric._openkb045 import CompilationError

COMMAND = [sys.executable, str(Path(__file__).parent / "fixtures/minimal_openkb.py")]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    kb, sources, run = (tmp_path / name for name in ("kb", "sources", "run"))
    for directory in (".openkb", "raw", "wiki/sources", "wiki/summaries", "wiki/concepts", "wiki/entities"):
        (kb / directory).mkdir(parents=True, exist_ok=True)
    (kb / ".openkb/config.yaml").write_text("model: chatgpt/gpt-6-luna\nlanguage: en\nconcurrency: 1\n")
    (kb / ".openkb/hashes.json").write_text("{}")
    (kb / ".gitignore").write_text(".openkb/\n")
    (kb / "wiki/AGENTS.md").write_text(AGENTS_MD)
    (kb / "wiki/index.md").write_text(INDEX_SEED)
    (kb / "wiki/log.md").write_text("# Operations Log\n")
    minimal.git(kb, "init", "-b", "main")
    minimal.git(kb, "config", "user.email", "offline@example.invalid")
    minimal.git(kb, "config", "user.name", "Offline")
    minimal.git(kb, "add", ".")
    minimal.git(kb, "commit", "-m", "baseline")
    sources.mkdir()
    for name in ("second.md", "first.md"):
        (sources / name).write_text("Synthetic facts about one and two. " + name)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "".join(
            json.dumps({"snapshot": name, "sha256": minimal.digest(sources / name)}) + "\n"
            for name in ("second.md", "first.md")
        )
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": "SYNTHETIC-SECRET", "account_id": "PRIVATE-ACCOUNT"}}
        )
    )
    settings = {
        "model": "gpt-6-luna",
        "effort": "xhigh",
        "max_requests": 50,
        "max_tokens": 2000000,
        "timeout": 120,
        "output_tokens": 20000,
        "auth_file": str(auth),
    }
    return kb, sources, manifest, run, settings


def launch(setup, **kwargs):
    kb, sources, manifest, run, settings = setup
    return minimal.run(kb, sources, manifest, run, settings=settings, command=COMMAND, **kwargs)


def test_publication_preserves_trailing_spaces_and_markdown_hard_breaks(setup):
    kb, *_ = setup
    page = kb / "wiki/concepts/trailing.md"
    content = "# Concept\n\nHard break.  \nTrailing space. \nFinal sentence.\n"
    page.write_text(content)
    minimal.git(kb, "config", "core.whitespace", "blank-at-eol,blank-at-eof,space-before-tab")
    head, outputs = minimal.commit(kb, "synthetic.md")
    assert page.read_text() == content
    assert outputs["wiki/concepts/trailing.md"] == minimal.digest(page)
    assert (
        subprocess.check_output(["git", "-C", str(kb), "show", head + ":wiki/concepts/trailing.md"]) == content.encode()
    )


def test_publication_still_rejects_conflict_markers(setup):
    kb, *_ = setup
    (kb / "wiki/concepts/conflict.md").write_text("<<<<<<< ours\nText\n=======\nOther\n>>>>>>> theirs\n")
    before = minimal.git(kb, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        minimal.commit(kb, "synthetic.md")
    assert minimal.git(kb, "rev-parse", "HEAD") == before


def test_success_order_receipts_stale_registry_and_no_live_calls(setup):
    kb, sources, manifest, run, settings = setup
    HashRegistry(kb / ".openkb/hashes.json").add(
        HashRegistry.hash_file(sources / "second.md"),
        {"doc_name": "second", "name": "second.md", "path": str(sources / "second.md")},
    )
    before, head = minimal.tree(kb), minimal.git(kb, "rev-parse", "HEAD")
    state = launch(setup)
    assert state["state"] == "READY_FOR_REVIEW"
    receipts = [json.loads((run / r["receipt"]).read_text()) for r in state["completed"]]
    assert [r["source"]["snapshot"] for r in receipts] == ["second.md", "first.md"]
    assert all(len(r["native"]["sources"][0]["required"]) == 2 for r in receipts)
    assert receipts[0]["parent_commit"] == head and receipts[1]["parent_commit"] == receipts[0]["commit"]
    assert minimal.git(run / "candidate", "status", "--porcelain") == ""
    assert minimal.tree(kb) == before and minimal.git(kb, "rev-parse", "HEAD") == head
    budget = check_budget(run)
    assert budget["requests"] > 2 and budget["tokens"] == budget["observed_tokens"] == 7 * budget["requests"]
    assert launch(setup, resume=True) == state and check_budget(run) == budget
    assert "SYNTHETIC-SECRET" not in "".join(p.read_text() for p in run.glob("*.json"))


def test_baseline_already_on_minimal_branch_is_preserved(setup):
    kb, _, _, run, _ = setup
    minimal.git(kb, "checkout", "-b", "codex/minimal")
    head, outputs = minimal.git(kb, "rev-parse", "HEAD"), minimal.tree(kb)
    refs = minimal.git(kb, "show-ref")
    assert launch(setup)["state"] == "READY_FOR_REVIEW"
    assert minimal.git(run / "candidate", "branch", "--show-current") == "codex/minimal"
    assert minimal.git(kb, "branch", "--show-current") == "codex/minimal"
    assert minimal.git(kb, "rev-parse", "HEAD") == head and minimal.tree(kb) == outputs
    assert minimal.git(kb, "show-ref") == refs


def test_cli_uses_qualified_post_response_output_acceptance(setup, monkeypatch):
    kb, sources, manifest, run, _ = setup
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["settings"])
        return {"state": "READY_FOR_REVIEW", "commit": "offline"}

    monkeypatch.setattr(minimal, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "minimal",
            "--kb",
            str(kb),
            "--source-dir",
            str(sources),
            "--manifest",
            str(manifest),
            "--run-root",
            str(run),
            "--max-requests",
            "50",
            "--max-tokens",
            "2000000",
        ],
    )
    assert minimal.main() == 0
    assert captured["output_tokens"] == 32000
    assert (captured["max_requests"], captured["max_tokens"]) == (50, 2000000)


@pytest.mark.parametrize("arguments", [[], ["one"], ["one", "two"], ["one", "two", "three", "four"]])
def test_internal_worker_rejects_wrong_argument_count(monkeypatch, capsys, arguments):
    monkeypatch.setattr(sys, "argv", ["minimal", "--worker", *arguments])
    assert minimal.main() == 2
    assert "Internal worker requires" in capsys.readouterr().err


def test_original_html_uses_native_conversion_and_preserves_raw_bytes(setup):
    kb, sources, manifest, run, settings = setup
    source = sources / "book.html"
    source.write_text("<html><body><p>Synthetic Athena speaks to Telemachus.</p></body></html>")
    manifest.write_text(json.dumps({"snapshot": source.name, "sha256": minimal.digest(source)}) + "\n")
    result = launch(setup)
    assert result["state"] == "READY_FOR_REVIEW"
    assert (run / "candidate/raw/book.html").read_bytes() == source.read_bytes()
    assert "Athena speaks to Telemachus" in (run / "candidate/wiki/sources/book.md").read_text()


def illustrated(setup, text=None):
    from PIL import Image

    _, sources, manifest, _, _ = setup
    for name, color in [("a/plot.png", "red"), ("b/plot.png", "blue"), ("figures/space one.png", "green")]:
        path = sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3), color).save(path)
    (sources / "figures/vector.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="3"/>')
    source = sources / "second.md"
    source.write_text(
        text
        or """# Illustrated source

![First](a/plot.png)
![Other](b/plot.png)
![Again](a/plot.png)
![Title](<figures/space one.png#detail> "A title")
![Reference][figure]

[figure]: figures/vector.svg "Reference title"

<img src="figures/space%20one.png" alt="HTML figure">

`![Example](missing-inline.png)`

<!-- ![Example](missing-comment.png) -->

```markdown
![Example](missing-fenced.png)
```
"""
    )
    from research_fabric._minimal_assets import local_image, references

    assets = {
        local_image(row["target"]): minimal.digest(sources / local_image(row["target"])) for row in references(source)
    }
    rows = [
        {"snapshot": "second.md", "sha256": minimal.digest(source), "assets": assets},
        {"snapshot": "first.md", "sha256": minimal.digest(sources / "first.md")},
    ]
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def test_real_native_images_preserve_hashes_links_resume_and_browser_export(setup):
    from research_fabric._minimal_assets import local_image, references, verify_files
    from research_fabric._source_assets import export_assets

    kb, sources, _, run, _ = setup
    rows = illustrated(setup)
    before = minimal.tree(sources)
    state = launch(setup)
    assert state["state"] == "READY_FOR_REVIEW" and state["image_interpretation"] == "NOT_QUALIFIED"
    assert minimal.tree(sources) == before
    receipt = json.loads((run / state["completed"][0]["receipt"]).read_text())
    verify_files(run / "candidate", receipt["preserved_assets"])
    for name, sha in rows[0]["assets"].items():
        assert minimal.digest(run / "candidate/raw" / name) == sha
    assert (run / "candidate/raw/second.md").read_bytes() == (sources / "second.md").read_bytes()
    assert not list((run / "candidate/raw").rglob("missing*"))
    head = minimal.git(run / "candidate", "rev-parse", "HEAD")
    for name, sha in receipt["preserved_assets"].items():
        import hashlib

        blob = subprocess.check_output(["git", "-C", str(run / "candidate"), "show", f"{head}:{name}"])
        assert hashlib.sha256(blob).hexdigest() == sha
    docs = run / "browser/docs"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "tools/wiki/build_wiki.py"),
            "--vault",
            str(run / "candidate/wiki"),
            "--docs",
            str(docs),
        ],
        check=True,
        capture_output=True,
    )
    original_refs = references(sources / "second.md")
    rendered = docs / "sources/second.md"
    assert len(references(rendered)) == len(original_refs)
    for original, image in zip(original_refs, references(rendered)):
        assert (
            minimal.digest(rendered.parent / local_image(image["target"]))
            == rows[0]["assets"][local_image(original["target"])]
        )
    budget = check_budget(run)
    assert launch(setup, resume=True) == state and check_budget(run) == budget
    for path in [sources / "a/plot.png", run / "candidate/wiki/sources/a/plot.png"]:
        original = path.read_bytes()
        path.write_bytes(b"changed image")
        with pytest.raises(CompilationError):
            launch(setup, resume=True)
        assert check_budget(run) == budget
        path.write_bytes(original)
    page = run / "candidate/wiki/sources/second.md"
    page.write_text(page.read_text() + "\n![Tampered](a/nonexistent.png)\n")
    with pytest.raises(CompilationError, match="hash mismatch"):
        export_assets(run / "candidate/wiki", run / "bad-export")
    assert minimal.tree(kb) != minimal.tree(run / "candidate")


@pytest.mark.parametrize("fault", ["missing", "hash", "undeclared", "extra", "symlink", "parent-symlink"])
def test_image_manifest_faults_stop_before_model_or_candidate(setup, fault):
    _, sources, manifest, run, _ = setup
    rows = illustrated(setup, "![Figure](a/plot.png)\n")
    asset = sources / "a/plot.png"
    if fault == "missing":
        asset.unlink()
    elif fault == "hash":
        asset.write_bytes(b"changed")
    elif fault == "undeclared":
        rows[0].pop("assets")
    elif fault == "extra":
        rows[0]["assets"]["b/plot.png"] = minimal.digest(sources / "b/plot.png")
    elif fault == "symlink":
        asset.unlink()
        asset.symlink_to(sources / "b/plot.png")
    else:
        (sources / "a").rename(sources / "actual-a")
        (sources / "a").symlink_to(sources / "actual-a", target_is_directory=True)
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(CompilationError):
        launch(setup)
    assert not run.exists()


@pytest.mark.parametrize(
    "target",
    ["../plot.png", "%2e%2e/plot.png", "/plot.png", "https://example.com/plot.png", "data:image/png;base64,AA=="],
)
def test_remote_embedded_and_escaping_images_fail_closed_offline(setup, target):
    _, sources, manifest, run, _ = setup
    source = sources / "second.md"
    source.write_text(f"![Figure]({target})\n")
    manifest.write_text(json.dumps({"snapshot": source.name, "sha256": minimal.digest(source)}) + "\n")
    with pytest.raises(CompilationError):
        launch(setup)
    assert not run.exists()


@pytest.mark.parametrize("fault", ["corrupt-image", "lost-image", "damage-previous-image", "existing-image-conflict"])
def test_native_image_failure_rolls_back_with_prior_images_intact(setup, monkeypatch, fault):
    kb, _, _, run, _ = setup
    rows = illustrated(setup, "![Figure](a/plot.png)\n")
    if fault == "existing-image-conflict":
        asset = kb / "wiki/sources/a/plot.png"
        asset.parent.mkdir()
        asset.write_bytes(b"existing image must not be overwritten")
        minimal.git(kb, "add", "wiki")
        minimal.git(kb, "commit", "-m", "Existing source asset")
    original = minimal.write_json

    def write(path, value):
        original(path, value)
        if path.name == "budget.json":
            (run / "fault").write_text(fault)

    monkeypatch.setattr(minimal, "write_json", write)
    with pytest.raises(CompilationError):
        launch(setup)
    state = json.loads((run / "run.json").read_text())
    assert state["state"] == "FAILED"
    if fault == "damage-previous-image":
        assert len(state["completed"]) == 1
        assert minimal.digest(run / "candidate/wiki/sources/a/plot.png") == rows[0]["assets"]["a/plot.png"]
    else:
        assert not state["completed"] and minimal.tree(run / "candidate") == minimal.tree(kb)


def test_original_html_images_preserve_raw_and_converted_links(setup):
    from research_fabric._minimal_assets import published_assets

    _, sources, manifest, run, _ = setup
    illustrated(setup, "![Figure](a/plot.png)\n")
    source = sources / "illustrated.html"
    source.write_text('<h1>Illustrated HTML</h1><img src="a/plot.png" alt="First"><img src="b/plot.png" alt="Other">')
    row = {
        "snapshot": source.name,
        "sha256": minimal.digest(source),
        "assets": {name: minimal.digest(sources / name) for name in ("a/plot.png", "b/plot.png")},
    }
    manifest.write_text(json.dumps(row) + "\n")
    state = launch(setup)
    assert state["state"] == "READY_FOR_REVIEW"
    receipt = json.loads((run / state["completed"][0]["receipt"]).read_text())
    assert published_assets(run / "candidate", source, row, "illustrated") == receipt["preserved_assets"]


def test_inherited_source_assets_cannot_be_changed_in_a_new_run(setup, monkeypatch):
    kb, _, _, run, _ = setup
    (kb / "wiki/sources/previous.png").write_bytes(b"previous accepted image")
    minimal.git(kb, "add", "wiki")
    minimal.git(kb, "commit", "-m", "Inherited image")
    before = minimal.tree(kb)
    original = minimal.write_json

    def write(path, value):
        original(path, value)
        if path.name == "budget.json":
            (run / "fault").write_text("damage-baseline-image")

    monkeypatch.setattr(minimal, "write_json", write)
    with pytest.raises(CompilationError, match="Preserved asset hash mismatch"):
        launch(setup)
    assert minimal.tree(run / "candidate") == before
    assert json.loads((run / "run.json").read_text())["state"] == "FAILED"


def test_native_copy_of_inert_nonimage_is_not_accepted(setup):
    _, sources, manifest, _, _ = setup
    source = sources / "second.md"
    source.write_text("Example: `![not a figure](second.md)`\n")
    row = {"snapshot": source.name, "sha256": minimal.digest(source)}
    manifest.write_text(json.dumps(row) + "\n")
    with pytest.raises(CompilationError):
        launch(setup)


def test_generated_reference_style_image_links_are_checked(setup):
    kb, *_ = setup
    (kb / "wiki/concepts/figure.md").write_text('![Figure][image]\n\n[image]: <../sources/missing image.png> "title"\n')
    with pytest.raises(CompilationError, match="Broken local link"):
        minimal.validate_links(kb)


@pytest.mark.parametrize("suffix", [".md", ".html"])
def test_responsive_image_sources_are_rejected_before_generation(setup, suffix):
    _, sources, manifest, run, _ = setup
    rows = illustrated(setup, "![Figure](a/plot.png)\n")
    source = sources / ("second" + suffix)
    source.write_text('<picture><source srcset="unchecked.png 2x"><img src="a/plot.png"></picture>\n')
    rows[0].update(snapshot=source.name, sha256=minimal.digest(source))
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(CompilationError, match="srcset"):
        launch(setup)
    assert not run.exists()


def test_image_paths_are_parsed_as_utf8_even_under_a_different_locale(setup, monkeypatch):
    _, sources, _, _, _ = setup
    source = sources / "second.md"
    source.write_text("![Figure](figures/διάγραμμα.png)\n", encoding="utf-8")
    read_text = Path.read_text

    def read_with_legacy_default(path, *args, **kwargs):
        if path == source and kwargs.get("encoding") is None:
            kwargs["encoding"] = "latin-1"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_with_legacy_default)
    from research_fabric._minimal_assets import local_image, references

    assert local_image(references(source)[0]["target"]) == "figures/διάγραμμα.png"


def test_benign_parent_segments_preserve_images_instead_of_rejecting_them(setup):
    from research_fabric._minimal_assets import local_image, safe_file

    _, sources, manifest, run, _ = setup
    rows = illustrated(setup, "![Figure](a/../b/plot.png)\n")
    assert rows[0]["assets"] == {"b/plot.png": minimal.digest(sources / "b/plot.png")}
    assert local_image("a/%2e%2e/b/plot.png") == "b/plot.png"
    assert safe_file(sources, "a/../b/plot.png") == (sources / "b/plot.png").resolve()
    state = launch(setup)
    assert state["state"] == "READY_FOR_REVIEW"
    assert minimal.digest(run / "candidate/raw/b/plot.png") == rows[0]["assets"]["b/plot.png"]
    assert json.loads(manifest.read_text().splitlines()[0])["assets"] == rows[0]["assets"]


@pytest.mark.parametrize("fault", ["missing-write", "summary-write", "malformed", "broken-link", "timeout", "quota"])
def test_failed_native_transaction_restores_entire_candidate_and_stays_not_ready(setup, monkeypatch, fault):
    kb, sources, manifest, run, settings = setup
    before = minimal.tree(kb)
    if fault == "timeout":
        settings["timeout"] = 1
    original = subprocess.run

    def controlled(argv, *args, **kwargs):
        if "--worker" in argv:
            (run / "fault").write_text(fault)
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", controlled)
    with pytest.raises((CompilationError, subprocess.TimeoutExpired)):
        launch(setup)
    state = json.loads((run / "run.json").read_text())
    assert state["state"] == "FAILED" and not state["completed"]
    assert minimal.tree(run / "candidate") == before == minimal.tree(kb)
    assert (run / "attempt-0001/failed-candidate").is_dir()
    if fault == "broken-link":
        # Native writes completed: the parent link gate, not a writer error,
        # must quarantine this otherwise-complete compilation.
        assert json.loads((run / "attempt-0001/result.json").read_text())["state"] == "COMPLETE"
    if fault == "quota":
        budget = json.loads((run / "budget.json").read_text())
        assert budget["requests"] == 1 and budget["stopped"] == "authentication_or_quota"
        with pytest.raises(CompilationError, match="terminal"):
            launch(setup, resume=True)


def test_resume_retains_first_checkpoint_and_recompiles_failed_second_source(setup, monkeypatch):
    kb, sources, manifest, run, settings = setup
    original = subprocess.run

    def controlled(argv, *args, **kwargs):
        if "--worker" in argv and Path(argv[-2]).name == "first.md":
            (run / "fault").write_text("missing-write")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", controlled)
    with pytest.raises(CompilationError):
        launch(setup)
    state = json.loads((run / "run.json").read_text())
    assert len(state["completed"]) == 1 and state["state"] == "FAILED"
    first = state["completed"][0]
    budget = check_budget(run)
    (run / "attempt-0003").mkdir()  # Interrupted baseline copy, not yet in run.json.
    (run / "fault").unlink()
    monkeypatch.setattr(subprocess, "run", original)
    result = launch(setup, resume=True)
    assert result["completed"][0] == first and len(result["completed"]) == 2
    assert check_budget(run)["requests"] > budget["requests"]
    assert len(list(run.glob("attempt-*/receipt.json"))) == 2
    assert (run / "attempt-0002/failed-candidate").is_dir()
    assert (run / "attempt-0003").is_dir() and (run / "attempt-0004/receipt.json").is_file()


def test_swallowed_optional_summary_budget_stop_cannot_publish(setup):
    kb, sources, manifest, run, settings = setup
    # Initial summary, planner, two required pages; optional rewrite would be #5.
    settings["max_requests"] = 4
    with pytest.raises(CompilationError):
        launch(setup)
    budget = json.loads((run / "budget.json").read_text())
    state = json.loads((run / "run.json").read_text())
    assert budget["requests"] == 4 and budget["stopped"] == "budget_exhausted"
    assert state["state"] == "FAILED" and not state["completed"]
    assert minimal.tree(run / "candidate") == minimal.tree(kb)


def test_resume_rejects_drift(setup):
    kb, sources, manifest, run, settings = setup
    state = launch(setup)
    for path, changed in [
        (sources / "first.md", b"Changed input"),
        (run / state["completed"][0]["receipt"], b"{}"),
        (run / "candidate/wiki/index.md", b"Changed accepted output"),
        (run / "budget.json", b'{"requests":0,"tokens":0,"observed_tokens":0}'),
        (manifest, manifest.read_bytes() + b"\n"),
    ]:
        original = path.read_bytes()
        path.write_bytes(changed)
        with pytest.raises(CompilationError):
            launch(setup, resume=True)
        path.write_bytes(original)
    candidate, held = run / "candidate", run / "held-candidate"
    # Staging changes only the Git index, which the output hash tree omits.
    before_budget = check_budget(run)
    minimal.git(candidate, "rm", "--cached", "wiki/index.md")
    with pytest.raises(CompilationError, match="Candidate drift"):
        launch(setup, resume=True)
    assert check_budget(run) == before_budget
    minimal.git(candidate, "reset", "HEAD", "--", "wiki/index.md")
    minimal.git(candidate, "add", "-f", ".openkb/config.yaml")
    with pytest.raises(CompilationError, match="Candidate drift"):
        launch(setup, resume=True)
    assert check_budget(run) == before_budget
    minimal.git(candidate, "reset", "HEAD", "--", ".openkb/config.yaml")
    candidate.rename(held)
    candidate.symlink_to(held, target_is_directory=True)
    try:
        with pytest.raises(CompilationError, match="root must not be a symlink"):
            launch(setup, resume=True)
    finally:
        candidate.unlink()
        held.rename(candidate)
    settings["max_requests"] += 1
    with pytest.raises(CompilationError):
        launch(setup, resume=True)


def test_crash_recovery_restores_before_resuming_and_ignores_no_unverified_hash(setup, monkeypatch):
    kb, sources, manifest, run, settings = setup
    launch(setup)
    state = json.loads((run / "run.json").read_text())
    # Simulate a parent crash at the next attempt, with an independently retained baseline.
    import shutil

    attempt = run / "attempt-0003"
    attempt.mkdir()
    shutil.copytree(run / "candidate", attempt / "baseline")
    state.update(state="COMPILING", pending=attempt.name)
    (run / "candidate/.openkb/hashes.json").write_text('{"partial": true}')
    (run / "candidate/wiki/index.md").write_text("partial")
    (run / "run.json").write_text(json.dumps(state))
    result = launch(setup, resume=True)
    assert result["state"] == "READY_FOR_REVIEW"
    assert (attempt / "failed-candidate/wiki/index.md").read_text() == "partial"


@pytest.mark.parametrize("phase", ["absent", "partial"])
def test_interrupted_restore_can_itself_be_resumed(setup, phase):
    import shutil

    kb, sources, manifest, run, settings = setup
    attempt = run / "attempt-0001"
    attempt.mkdir(parents=True)
    shutil.copytree(kb, attempt / "baseline")
    shutil.copytree(kb, attempt / "failed-candidate")
    if phase == "partial":
        (run / "candidate").mkdir()
        (run / "candidate/partial-copy").write_text("Interrupted restore")
    state = {"pending": attempt.name, "outputs": minimal.tree(kb), "commit": minimal.git(kb, "rev-parse", "HEAD")}
    minimal._restore(run, state)
    assert state["state"] == "FAILED" and "pending" not in state
    assert minimal.tree(run / "candidate") == minimal.tree(kb)
    assert (attempt / "failed-candidate").is_dir()
    if phase == "partial":
        assert (attempt / "interrupted-restore-1/partial-copy").read_text() == "Interrupted restore"


def test_minimal_does_not_import_full_engine():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import research_fabric.minimal; "
            "assert not {'research_fabric.execution','research_fabric.engine','research_fabric.claims',"
            "'research_fabric.publication','research_fabric.compilation'} & sys.modules.keys()",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0


def test_inherited_child_lock_prevents_parent_crash_race(setup):
    import fcntl

    kb, sources, manifest, run, settings = setup
    run.mkdir()
    with (run / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen(
            [sys.executable, "-c", 'import time; print("ready", flush=True); time.sleep(30)'],
            pass_fds=(lock.fileno(),),
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout.readline().strip() == "ready"
    try:
        with pytest.raises(CompilationError, match="child still owns"):
            launch(setup, resume=True)
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("case", ["hash", "duplicate", "escape"])
def test_manifest_is_verified_before_any_candidate_or_model_call(setup, case):
    kb, sources, manifest, run, settings = setup
    if case == "hash":
        (sources / "first.md").write_text("source drift")
    elif case == "duplicate":
        manifest.write_text(manifest.read_text() * 2)
    else:
        manifest.write_text(json.dumps({"snapshot": "../outside.md", "sha256": "0" * 64}) + "\n")
    with pytest.raises(CompilationError):
        launch(setup)
    assert not run.exists()


@pytest.mark.parametrize("fault", ["ignored", "hook", "empty", "journal", "symlink", "late-change"])
def test_publication_and_preflight_faults_are_never_ready(setup, monkeypatch, fault):
    kb, sources, manifest, run, settings = setup
    if fault == "journal":
        journal = kb / ".openkb/journal/pending.json"
        journal.parent.mkdir()
        journal.write_text("{}")
    elif fault == "symlink":
        (kb / ".openkb/escape").symlink_to(sources / "first.md")
    elif fault == "ignored":
        (kb / ".gitignore").write_text(".openkb/\nwiki/concepts/\n")
        minimal.git(kb, "add", ".gitignore")
        minimal.git(kb, "commit", "-m", "Ignore future output")
    else:
        original = minimal.commit

        def commit(candidate, source):
            if fault == "hook":
                hook = candidate / ".git/hooks/pre-commit"
                hook.write_text("#!/bin/sh\nprintf 'altered\\n' > wiki/concepts/one.md\ngit add wiki/concepts/one.md\n")
                hook.chmod(0o755)
            elif fault == "late-change":
                (candidate / "wiki/concepts/one.md").write_text("Edited after native attestation\n")
            else:
                minimal.git(candidate, "add", "-A", "--", "wiki", "raw")
                minimal.git(candidate, "commit", "-m", "already committed before gate")
            return original(candidate, source)

        monkeypatch.setattr(minimal, "commit", commit)
    with pytest.raises((CompilationError, subprocess.CalledProcessError)):
        launch(setup)
    if (run / "run.json").exists():
        state = json.loads((run / "run.json").read_text())
        assert state["state"] == "FAILED"
        assert minimal.tree(run / "candidate") == minimal.tree(kb)


@pytest.mark.parametrize("case", ["limit", "endpoint", "effort", "unknown"])
def test_transport_is_bounded_and_never_falls_back(setup, monkeypatch, case):
    import httpx

    from research_fabric._minimal_transport import subscription, write_json

    kb, sources, manifest, run, settings = setup
    run.mkdir()
    write_json(run / "budget.json", {"requests": 0, "tokens": 0, "observed_tokens": 0})
    settings["max_requests"] = 1
    calls = []

    def send(client, request, **kwargs):
        calls.append(request)
        payload = {"type": "response.completed", "response": {"model": "gpt-6-luna"}}
        if case != "unknown":
            payload["response"]["usage"] = {"total_tokens": 7, "output_tokens": 4}
        return httpx.Response(200, request=request, text="data: " + json.dumps(payload) + "\n\n")

    monkeypatch.setattr(httpx.Client, "send", send)
    endpoint = (
        "https://api.openai.com/v1/responses"
        if case == "endpoint"
        else "https://chatgpt.com/backend-api/codex/responses"
    )
    body = {"model": "gpt-6-luna", "reasoning": {"effort": "high" if case == "effort" else "xhigh"}}
    with pytest.raises(CompilationError), subscription(run, settings), httpx.Client() as client:
        client.post(endpoint, json=body)
        client.post(endpoint, json=body)
    state = json.loads((run / "budget.json").read_text())
    assert state["stopped"] and len(calls) == (0 if case in ("endpoint", "effort") else 1)
    assert state["requests"] == len(calls)
    if case == "unknown":
        assert state["tokens"] >= 131072 and state["observed_tokens"] == 0


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("explicit_follow", [False, True])
def test_transport_never_follows_unaccounted_redirects(setup, asynchronous, status, explicit_follow):
    import asyncio

    import httpx

    from research_fabric._minimal_transport import subscription, write_json

    _, _, _, run, settings = setup
    run.mkdir()
    write_json(run / "budget.json", {"requests": 0, "tokens": 0, "observed_tokens": 0})
    endpoint = "https://chatgpt.com/backend-api/codex/responses"
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://other.example/collect"}, request=request)

    options = {"follow_redirects": True} if explicit_follow else {}
    body = {"model": "gpt-6-luna", "reasoning": {"effort": "xhigh"}, "input": "PRIVATE CORPUS"}
    transport = httpx.MockTransport(redirect)

    async def send_async():
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            await client.post(endpoint, json=body, **options)

    with pytest.raises(CompilationError), subscription(run, settings):
        if asynchronous:
            asyncio.run(send_async())
        else:
            with httpx.Client(transport=transport, follow_redirects=True) as client:
                client.post(endpoint, json=body, **options)
    state = json.loads((run / "budget.json").read_text())
    assert calls == [endpoint]
    assert state["requests"] == 1 and state["stopped"] == "unknown_usage"
    assert state["tokens"] >= 131072 and state["observed_tokens"] == 0


@pytest.mark.parametrize("total", [0, 7])
def test_transport_reconciles_reservations_to_actual_usage(setup, monkeypatch, total):
    import httpx

    from research_fabric._minimal_transport import subscription, write_json

    _, _, _, run, settings = setup
    run.mkdir()
    write_json(run / "budget.json", {"requests": 0, "tokens": 0, "observed_tokens": 0})

    def send(client, request, **kwargs):
        payload = {
            "type": "response.completed",
            "response": {"model": "gpt-6-luna", "usage": {"total_tokens": total, "output_tokens": 0}},
        }
        return httpx.Response(200, request=request, text="data: " + json.dumps(payload) + "\n\n")

    monkeypatch.setattr(httpx.Client, "send", send)
    with subscription(run, settings), httpx.Client() as client:
        for _ in range(2):
            client.post(
                "https://chatgpt.com/backend-api/codex/responses",
                json={"model": "gpt-6-luna", "reasoning": {"effort": "xhigh"}},
            )
    state = check_budget(run)
    assert state["requests"] == 2
    assert state["tokens"] == state["observed_tokens"] == 2 * total
