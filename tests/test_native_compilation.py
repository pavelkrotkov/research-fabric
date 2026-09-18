"""Qualified native compiler tests: stub only external model responses, no keys."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("openkb", reason="Install native-compile extra to qualify native adapter")
from openkb import cli
from openkb.agent import compiler
from openkb.schema import AGENTS_MD, INDEX_SEED

from research_fabric._openkb045 import CompilationError, native_compile, strict_native


@pytest.fixture
def kb(tmp_path):
    root = tmp_path / "kb"
    for directory in (".openkb", "raw", "wiki/sources", "wiki/summaries", "wiki/concepts", "wiki/entities"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / ".openkb/config.yaml").write_text("model: openai/test\nlanguage: en\nconcurrency: 1\n")
    (root / ".openkb/hashes.json").write_text("{}")
    (root / "wiki/AGENTS.md").write_text(AGENTS_MD)
    (root / "wiki/index.md").write_text(INDEX_SEED)
    (root / "wiki/log.md").write_text("# Operations Log\n")
    return root


def response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=5),
    )


@pytest.fixture
def model(monkeypatch):
    state = {"failure": None, "calls": [], "models": [], "plan": {"create": [{"name": "one"}, {"name": "two"}]}}

    def sync(model, messages, **kwargs):
        state["models"].append(model)
        prompt = messages[-1]["content"]
        if isinstance(prompt, list):
            prompt = prompt[0]["text"]
        if "decide how to update" in prompt:
            return response(json.dumps(state["plan"]))
        if "Rewrite the summary" in prompt:
            if state["failure"] == "summary":
                raise TimeoutError("optional rewrite")
            return response("Final summary")
        return response(json.dumps({"description": "Synthetic source", "content": "Initial summary"}))

    async def async_call(model, messages, **kwargs):
        state["models"].append(model)
        await asyncio.sleep(0.001)  # Exercise real coroutine suspension.
        prompt = messages[-1]["content"]
        state["calls"].append(prompt)
        if state["failure"] == "timeout" and "for: two" in prompt:
            raise TimeoutError("required page timeout")
        return response(json.dumps({"description": "Concept", "content": "Native generated body"}))

    monkeypatch.setattr(compiler.litellm, "completion", sync)
    monkeypatch.setattr(compiler.litellm, "acompletion", async_call)
    return state


def source(tmp_path, name="source.md"):
    path = tmp_path / name
    path.write_text("Synthetic facts about one and two.")
    return path


def test_native_success_and_optional_summary_fallback(kb, model, tmp_path):
    model["failure"] = "summary"
    report = native_compile(kb, [source(tmp_path)])
    assert len(report["sources"][0]["required"]) == 2
    assert report["sources"][0]["optional_summary"] == "native fallback"
    assert len(model["calls"]) == 2
    assert "Native generated body" in (kb / "wiki/concepts/one.md").read_text()


def test_native_timeout_rolls_back_and_identical_retry_processes(kb, model, tmp_path):
    src = source(tmp_path)
    original = "---\nsources: [summaries/old.md]\n---\nOld content"
    (kb / "wiki/concepts/one.md").write_text(original)
    model["plan"] = {"update": [{"name": "one"}, {"name": "two"}]}
    model["failure"] = "timeout"
    with strict_native(compiler, cli):
        assert cli.add_single_file(src, kb) == "failed"
    assert (kb / "wiki/concepts/one.md").read_text() == original
    assert not (kb / "wiki/summaries/source.md").exists()
    assert json.loads((kb / ".openkb/hashes.json").read_text()) == {}
    model["failure"] = None
    assert native_compile(kb, [src])["sources"]
    assert len(model["calls"]) == 4


def test_native_directory_aggregation_fails(kb, model, tmp_path):
    model["failure"] = "timeout"
    with pytest.raises(CompilationError, match="did not complete"):
        native_compile(kb, [source(tmp_path, "first.md"), source(tmp_path, "second.md")])
    assert json.loads((kb / ".openkb/hashes.json").read_text()) == {}


def test_native_write_failure_rolls_back(kb, model, tmp_path, monkeypatch):
    original = compiler.atomic_write_text

    def failing(path, content, **kwargs):
        if Path(path).name == "two.md":
            raise OSError("disk full")
        original(path, content, **kwargs)

    monkeypatch.setattr(compiler, "atomic_write_text", failing)
    with pytest.raises(CompilationError):
        native_compile(kb, [source(tmp_path)])
    assert not (kb / "wiki/concepts/one.md").exists()
    assert json.loads((kb / ".openkb/hashes.json").read_text()) == {}


def test_native_noop_writer_is_not_an_applied_operation(kb, model, tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "_write_concept", lambda *a, **kw: None)
    with pytest.raises(CompilationError):
        native_compile(kb, [source(tmp_path)])


def test_native_plan_normalization_and_related(kb, model, tmp_path):
    (kb / "wiki/concepts/existing.md").write_text("---\nsources: []\n---\nExisting concept")
    model["plan"] = {"create": [None, {"name": "one", "title": "One"}], "related": ["nonexistent", "existing"]}
    report = native_compile(kb, [source(tmp_path)])
    assert report["sources"][0]["required"] == [("concepts", "create", "one"), ("concepts", "related", "existing")]
    assert len(model["calls"]) == 1
    assert "[[summaries/source]]" in (kb / "wiki/concepts/existing.md").read_text()


def test_native_missing_plan_fails_closed(kb, model, tmp_path):
    model["plan"] = "not a plan"
    with pytest.raises(CompilationError):
        native_compile(kb, [source(tmp_path)])


def test_production_native_retry_starts_from_baseline(kb, model, tmp_path):
    import subprocess
    import sys

    from research_fabric.compilation import compile_with_recovery

    def git(*args):
        return subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True)

    git("init", "-b", "agent/test")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-m", "baseline")
    src = source(tmp_path)
    counter = tmp_path / "calls.json"
    command = [sys.executable, str(Path(__file__).parent / "fixtures/scripted_openkb.py"), str(counter)]
    report = compile_with_recovery(kb, [src], tmp_path / "diagnostics", command=command)
    assert len(report["sources"][0]["required"]) == 2
    assert json.loads(counter.read_text()) == {"attempts": 2, "pages": 4}
    assert (kb / "wiki/concepts/two.md").is_file()
    failed = next((tmp_path / "diagnostics").glob("*/candidate-1"))
    assert not (failed / "wiki/concepts/one.md").exists()
    assert json.loads((failed / ".openkb/hashes.json").read_text()) == {}


@pytest.mark.parametrize(
    "ignored,collect_new", [(None, False), ("wiki/concepts/", False), ("evidence/snapshots/", False), (None, True)]
)
def test_real_workflow_reaches_ready_with_native_compile_and_real_gates(
    kb, tmp_path, monkeypatch, ignored, collect_new
):
    """CAO transport and provider are synthetic; workflow, compiler and gates are real."""
    import hashlib
    import os
    import runpy
    import subprocess
    import sys
    import types

    from research_fabric import compilation

    root = Path(__file__).resolve().parents[1]
    if collect_new:
        (kb / ".openkb/config.yaml").write_text("language: en\nconcurrency: 1\n")
    if ignored:
        (kb / ".gitignore").write_text(ignored + "\n")
    for args in (
        ("init", "-b", "agent/workflow"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-m", "baseline"),
    ):
        subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True)
    sources = tmp_path / "sources"
    sources.mkdir()
    src = sources / "odyssey-book-1.html"
    excerpt = "The glaucous-eyed goddess Athena spoke to Telemachus."
    src.write_text(f"<html><body><p>{excerpt}</p></body></html>")
    run = tmp_path / "workflow-run"
    run.mkdir()
    manifest = {
        "source_id": "s-odyssey-1-theoi",
        "snapshot": src.name,
        "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "url": "https://example.invalid/synthetic",
        "title": "Synthetic Book",
        "retrieved_at": "2026-01-01",
        "content_type": "text/html",
    }
    (run / "source-manifest.jsonl").write_text(json.dumps(manifest) + "\n")
    reused = tmp_path / "reuse"
    reused.mkdir()
    packet = {
        "claims": [
            {
                "claim": f"Athena speaks to Telemachus, observation {i}.",
                "source_file": src.name,
                "locator": "Book 1",
                "excerpt": excerpt,
                "stance": "supports",
                "confidence": 0.9,
                "uncertainty": "low",
            }
            for i in range(5)
        ],
        "conflicts": [],
        "coverage_notes": [],
    }
    (reused / "worker-book-1.json").write_text(json.dumps({"parsed": packet}))
    shim = types.ModuleType("cao_workflow")
    shim.ShimError = RuntimeError
    shim.get_inputs = lambda: {
        "field_root": str(kb),
        "run_root": str(run),
        "source_dir": str(sources),
        "question": "What happened?",
        "reuse_evidence_dir": None if collect_new else str(reused),
    }
    outputs = []
    shim.emit_output = outputs.append
    shim.run_step = lambda **kwargs: SimpleNamespace(output="VERDICT: FAIL - advisory fixture")
    monkeypatch.setitem(sys.modules, "cao_workflow", shim)
    monkeypatch.setenv("RESEARCH_FABRIC_ROOT", str(root))
    if collect_new:
        wrapper = tmp_path / "worker-python"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            + """
import json, runpy, sys
import httpx
import openai
Original = openai.OpenAI
"""
            + f"packet = {packet!r}\n"
            + """
def respond(request):
    return httpx.Response(200, json={"id":"fixture", "object":"chat.completion", "created":1,
        "model":"actual-evidence-model", "choices":[{"index":0,"finish_reason":"stop",
        "message":{"role":"assistant","content":json.dumps(packet)}}],
        "usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}})
def client(**kwargs):
    kwargs["api_key"] = "offline-fixture"
    return Original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
openai.OpenAI = client
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture")
        monkeypatch.setenv("RESEARCH_FABRIC_WORKER_PYTHON", str(wrapper))
    executables = tmp_path / "bin"
    executables.mkdir()
    lint = executables / "openkb"
    lint.write_text(f"""#!{sys.executable}
from openkb import cli
from openkb.agent import linter
async def advisory(*args, **kwargs):
    return "Advisory fixture: no live provider"
linter.run_knowledge_lint = advisory
cli.cli()
""")
    lint.chmod(0o755)
    monkeypatch.setenv("PATH", str(executables) + os.pathsep + os.environ["PATH"])
    production_compile = compilation.compile_with_recovery
    command = [sys.executable, str(root / "tests/fixtures/scripted_openkb.py"), str(tmp_path / "calls.json")]
    monkeypatch.setattr(
        compilation,
        "compile_with_recovery",
        lambda kb, src, diag, **kwargs: production_compile(kb, src, diag, command=command, **kwargs),
    )
    if ignored:
        with pytest.raises(CompilationError, match="ignored/untracked"):
            runpy.run_path(str(root / "workflows/research.py"), run_name="__main__")
        assert json.loads((run / "run.json").read_text())["state"] == "FAILED"
        assert outputs == []
        assert subprocess.check_output(["git", "-C", str(kb), "rev-list", "--count", "HEAD"]).strip() == b"1"
        return
    runpy.run_path(str(root / "workflows/research.py"), run_name="__main__")
    result = json.loads((run / "run.json").read_text())
    assert result["state"] == "READY_FOR_REVIEW"
    assert (
        result["provenance"]["corpus_manifest_sha"]
        == hashlib.sha256((run / "source-manifest.jsonl").read_bytes()).hexdigest()
    )
    assert result["claims"] == 5
    execution = result["provenance"]["execution"]
    assert execution["attempts"]
    assert {row["role"] for row in execution["attempts"]} == ({"compile", "extraction"} if collect_new else {"compile"})
    if collect_new:
        from openkb.config import DEFAULT_CONFIG

        native_rows = [row for row in execution["attempts"] if row["role"] == "compile"]
        assert {row["profile"]["model"] for row in native_rows} == {DEFAULT_CONFIG["model"]}
        packet_execution = json.loads((run / "evidence/worker-book-1.json").read_text())["execution"]
        assert packet_execution[0]["actual_model"] == "actual-evidence-model"
        ledger = json.loads((kb / "evidence/claims.jsonl").read_text().splitlines()[0])
        assert ledger["execution"] == packet_execution
    assert result["provenance"]["advisory_execution"]["actual_model"] is None
    assert outputs[-1]["commit"] == result["commit"]
    assert (run / "verification/provenance.txt").exists()
    assert (run / "verification/excerpt-grounding.txt").exists()
    assert subprocess.check_output(["git", "-C", str(kb), "status", "--porcelain"]) == b""


def test_stale_native_registry_is_not_reused(kb, model, tmp_path):
    from openkb.state import HashRegistry

    src = source(tmp_path)
    HashRegistry(kb / ".openkb/hashes.json").add(
        HashRegistry.hash_file(src), {"doc_name": "source", "name": src.name, "path": str(src)}
    )
    report = native_compile(kb, [src])
    assert len(model["calls"]) == 2
    assert len(report["sources"][0]["required"]) == 2


def test_unqualified_native_source_fails_before_writing(kb, model, tmp_path, monkeypatch):
    from research_fabric import _openkb045

    monkeypatch.setattr(_openkb045, "COMPILER_SHA", "unqualified")
    with pytest.raises(CompilationError, match="source differs"):
        native_compile(kb, [source(tmp_path)])
    assert model["calls"] == []
    assert json.loads((kb / ".openkb/hashes.json").read_text()) == {}


def test_native_fallback_uses_fresh_candidate_and_shared_accounting(kb, tmp_path):
    import subprocess
    import sys

    from research_fabric.compilation import compile_with_recovery
    from research_fabric.execution import configure, history, resolve

    for args in (
        ("init", "-b", "agent/execution"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-m", "baseline"),
    ):
        subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True)
    run = tmp_path / "run"
    configure(
        run,
        resolve(
            override={"fallbacks": {"compile": [{"provider": "openai", "model": "C"}]}},
            native_model="openai/B",
            environ={},
        ),
    )
    command = [sys.executable, str(Path(__file__).parent / "fixtures/scripted_openkb.py"), str(tmp_path / "calls.json")]
    compile_with_recovery(kb, [source(tmp_path)], tmp_path / "diagnostics", command=command, execution_root=run)
    records = [row for row in history(run)["attempts"] if row["role"] == "compile"]
    assert {row["profile"]["model"] for row in records} == {"B", "C"}
    assert sum(row["outcome"] == "transient" for row in records) >= 2
    assert all(row["revision"] == 1 for row in records)
    assert (kb / "wiki/concepts/two.md").is_file()
    failed = next((tmp_path / "diagnostics").glob("*/candidate-1"))
    assert not (failed / "wiki/concepts/one.md").exists()
    identity = json.loads(next((tmp_path / "diagnostics").glob("*/identity.json")).read_text())
    assert identity["execution"]["revision"] == 1
    assert set(identity["execution"]["code"]) == {
        "execution.py",
        "execution_native.py",
        "_execution_profiles.py",
        "_execution_journal.py",
    }


def test_native_exhausted_shared_budget_never_accepts_optional_fallback(kb, model, tmp_path):
    from research_fabric.execution import configure, history, resolve

    run = tmp_path / "run"
    configure(run, resolve(override={"budget": {"requests": 1}}, native_model="openai/B", environ={}))
    with pytest.raises((CompilationError, RuntimeError)):
        native_compile(kb, [source(tmp_path)], execution_root=run)
    records = history(run)["attempts"]
    assert len(records) == 1
    assert not (kb / "wiki/concepts/one.md").exists()


def test_native_oauth_route_is_passed_to_existing_native_transport(kb, model, tmp_path):
    from research_fabric.execution import configure, history, resolve

    run = tmp_path / "run"
    configure(run, resolve(native_model="chatgpt/gpt-5", environ={}))
    report = native_compile(kb, [source(tmp_path)], execution_root=run)
    assert report["sources"]
    records = history(run)["attempts"]
    assert all(row["profile"]["auth"] == "oauth" for row in records)
    assert all(row["profile"]["provider"] == "chatgpt" for row in records)
    assert all(row["actual_model"] is None for row in records)
    assert set(model["models"]) == {"chatgpt/gpt-5"}
