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
    "ignored,collect_new,policy,attestation,source_format",
    [
        (None, False, None, "valid", "html"),
        ("wiki/concepts/", False, None, "valid", "html"),
        ("evidence/snapshots/", False, None, "valid", "html"),
        (None, True, None, "valid", "html"),
        (None, False, "restricted-reuse", "valid", "html"),
        (None, True, "restricted-fresh", "valid", "html"),
        (None, False, "compatible-reuse", "valid", "html"),
        (None, False, "legacy-restricted", "valid", "html"),
        (None, False, None, "missing", "html"),
        (None, False, None, "stale", "html"),
        (None, False, None, "valid", "markdown"),
        (None, True, None, "valid", "markdown"),
    ],
)
def test_real_workflow_reaches_ready_with_native_compile_and_real_gates(
    kb, tmp_path, monkeypatch, ignored, collect_new, policy, attestation, source_format
):
    """CAO transport and provider are synthetic; workflow, compiler and gates are real."""
    import hashlib
    import os
    import runpy
    import subprocess
    import sys

    from research_fabric.engine import ResearchRun, RunConfig

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
    suffix = "md" if source_format == "markdown" else "html"
    src = sources / f"odyssey-book-1.{suffix}"
    excerpt = "The glaucous-eyed goddess Athena spoke to Telemachus."
    src.write_text(f"<html><body><p>{excerpt}</p></body></html>")
    if source_format == "markdown":
        from PIL import Image

        excerpt = "The sample temperature is $T = 21$ degrees."
        src.write_text(
            "---\ntitle: Measurement\n---\n\n# Measurement\n\n"
            + excerpt
            + "\n\n| T | 21 |\n| --- | --- |\n\n```text\nrecorded = 21\n```\n\n![Plot](plot.png)\n"
        )
        Image.new("RGB", (16, 16), "red").save(sources / "plot.png")
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
    if source_format == "markdown":
        from research_fabric.sources import representation_for

        manifest.update(content_type="text/markdown", assets_sha256=representation_for(src).assets_sha256)
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
    from research_fabric import execution as ex
    from research_fabric.sources import source_provenance

    reused_packet = {"parsed": packet, "source_provenance": source_provenance([src])}
    if attestation == "missing":
        del reused_packet["source_provenance"]
    elif attestation == "stale":
        reused_packet["source_provenance"][0]["sha256"] = "0" * 64
    if policy in ("restricted-reuse", "compatible-reuse"):
        prior = tmp_path / "prior-run"
        ex.configure(prior, ex.resolve(override={"roles": {"extraction": {"model": "forbidden-B"}}}, environ={}))
        session = ex.Session(prior, "extraction", "book-1", [src])
        attempt, _ = session.begin(session.profiles()[0])
        session.finish(attempt, SimpleNamespace(model="forbidden-B"), "accepted", 0.01)
        reused_packet["execution"] = session.records()
    if policy or source_format == "markdown":
        projects = tmp_path / "projects"
        projects.mkdir()
        restriction = "" if not policy or policy == "compatible-reuse" else "allowed_models: [allowed-C]\n"
        (projects / "odyssey.yaml").write_text(
            (root / "projects/odyssey.yaml").read_text()
            + "\n"
            + restriction
            + "execution:\n  roles:\n    extraction: {model: allowed-C}\n    repair: {model: allowed-C}\n"
        )
        if source_format == "markdown":
            project_path = projects / "odyssey.yaml"
            project_path.write_text(project_path.read_text().replace(r"\.html$", r"\.md$").replace(".html", ".md"))
        monkeypatch.setenv("RESEARCH_FABRIC_PROJECTS", str(projects))
    (reused / "worker-book-1.json").write_text(json.dumps(reused_packet))
    collected = collect_new or policy in ("restricted-reuse", "legacy-restricted")
    returned_model = (
        "forbidden-B" if policy == "restricted-fresh" else "allowed-C" if policy else "actual-evidence-model"
    )
    outputs = []
    if collected:
        wrapper = tmp_path / "worker-python"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            + """
import json, runpy, sys
import httpx
import openai
Original = openai.OpenAI
"""
            + f"packet = {packet!r}\nreturned_model = {returned_model!r}\n"
            + """
def respond(request):
    return httpx.Response(200, json={"id":"fixture", "object":"chat.completion", "created":1,
        "model":returned_model, "choices":[{"index":0,"finish_reason":"stop",
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

    def launch():
        config = RunConfig(
            engine_root=root,
            project_path=Path(os.environ.get("RESEARCH_FABRIC_PROJECTS", root / "projects")) / "odyssey.yaml",
            field_root=kb,
            run_root=run,
            source_dir=sources,
            question="What happened?",
            reuse_evidence_dir=None if collect_new else reused,
            worker_python=os.environ.get("RESEARCH_FABRIC_WORKER_PYTHON", sys.executable),
            compiler_command=(
                sys.executable,
                str(root / "tests/fixtures/scripted_openkb.py"),
                str(tmp_path / "calls.json"),
            ),
        )
        result = ResearchRun(
            config, lambda **_: "VERDICT: FAIL - advisory fixture", agent_provider="scripted"
        ).execute()
        outputs.append(result)
        return result

    if policy == "restricted-fresh":
        with pytest.raises(RuntimeError, match="evidence workers failed"):
            launch()
        assert json.loads((run / "run.json").read_text())["state"] == "FAILED"
        assert ex.history(run)["attempts"][0]["actual_model"] == "forbidden-B"
        assert not (tmp_path / "calls.json").exists()
        return
    if attestation != "valid":
        # An invalid reused packet must trigger collection, never compilation.
        # The unavailable executable makes that required collection fail offline.
        monkeypatch.setenv("RESEARCH_FABRIC_WORKER_PYTHON", str(tmp_path / "missing-worker"))
        with pytest.raises(RuntimeError, match="one or more evidence workers failed"):
            launch()
        assert json.loads((run / "run.json").read_text())["state"] == "FAILED"
        assert outputs == []
        assert not (tmp_path / "calls.json").exists()
        assert subprocess.check_output(["git", "-C", str(kb), "rev-list", "--count", "HEAD"]).strip() == b"1"
        return
    if ignored:
        with pytest.raises(CompilationError, match="ignored/untracked"):
            launch()
        assert json.loads((run / "run.json").read_text())["state"] == "FAILED"
        assert outputs == []
        assert subprocess.check_output(["git", "-C", str(kb), "rev-list", "--count", "HEAD"]).strip() == b"1"
        return
    launch()
    result = json.loads((run / "run.json").read_text())
    assert result["state"] == "READY_FOR_REVIEW"
    assert (
        result["provenance"]["corpus_manifest_sha"]
        == hashlib.sha256((run / "source-manifest.jsonl").read_bytes()).hexdigest()
    )
    assert result["provenance"]["source_adapters"] == (["markdown@2"] if source_format == "markdown" else ["html@1"])
    assert result["claims"] == 5
    execution = result["provenance"]["execution"]
    assert execution["attempts"]
    assert {row["role"] for row in execution["attempts"]} == ({"compile", "extraction"} if collected else {"compile"})
    if collect_new:
        from openkb.config import DEFAULT_CONFIG

        native_rows = [row for row in execution["attempts"] if row["role"] == "compile"]
        assert {row["profile"]["model"] for row in native_rows} == {DEFAULT_CONFIG["model"]}
    if collected:
        packet_execution = json.loads((run / "evidence/worker-book-1.json").read_text())["execution"]
        assert packet_execution[0]["actual_model"] == returned_model
        ledger = json.loads((kb / "evidence/claims.jsonl").read_text().splitlines()[0])
        published = json.loads((kb / "evidence/packet-execution.json").read_text())[ledger["worker"]]
        assert published == {"packet_revision": ledger["packet_revision"], "execution": packet_execution}
    if policy == "compatible-reuse":
        ledger = json.loads((kb / "evidence/claims.jsonl").read_text().splitlines()[0])
        published = json.loads((kb / "evidence/packet-execution.json").read_text())[ledger["worker"]]
        assert published == {"packet_revision": ledger["packet_revision"], "execution": reused_packet["execution"]}
    assert result["provenance"]["advisory_execution"]["actual_model"] is None
    accepted = json.loads((run / "evidence/worker-book-1.json").read_text())
    ledger = [json.loads(line) for line in (kb / "evidence/claims.jsonl").read_text().splitlines()]
    assert [row["claim_id"] for row in ledger] == accepted["claim_identity"]["claim_ids"]
    assert all(row["packet_revision"] == accepted["packet_revision"] for row in ledger)
    assert json.loads((kb / "evidence/claim-history.json").read_text()) == accepted["claim_history"]
    assert outputs[-1]["commit"] == result["commit"]
    assert subprocess.check_output(["git", "-C", str(kb), "rev-parse", "HEAD"]).decode().strip() == result["commit"]
    assert subprocess.check_output(["git", "-C", str(kb), "status", "--porcelain"]) == b""
    source_row = json.loads((kb / "evidence/sources.jsonl").read_text().splitlines()[0])
    snapshot = kb / source_row["snapshot"]
    assert snapshot.read_bytes() == src.read_bytes()
    if source_format == "markdown":
        assert (snapshot.parent / "plot.png").read_bytes() == (sources / "plot.png").read_bytes()
        from research_fabric._source_assets import export_assets

        assert len(export_assets(kb / "wiki", tmp_path / "docs")) == 2
        import shutil

        from research_fabric.compilation import _git, _tree
        from research_fabric.run_state import _publication_outputs

        shutil.copytree(sources, run / "sources")
        prepared = tmp_path / "prepared"
        _git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(kb), str(prepared))
        _git(prepared, "config", "user.email", "fixture@example.invalid")
        _git(prepared, "config", "user.name", "Fixture")
        # Retain actual native pages/assets/snapshots; remove derived publication records only.
        records = ("claims.jsonl", "sources.jsonl", "claim-history.json", "packet-execution.json")
        _git(prepared, "rm", "--", *(f"evidence/{name}" for name in records))
        _git(prepared, "commit", "-m", "Prepared native candidate without published ledgers")
        prepared_head = _git(prepared, "rev-parse", "HEAD")
        original_run = _tree(run)
        native_calls = (tmp_path / "calls.json").read_bytes()
        production_files = set(_git(kb, "ls-files", "--", "wiki", "evidence").splitlines())
        production_outputs = _publication_outputs(kb)
        observed = {}
        rehearsal = runpy.run_path(str(root / "bin/dryrun_publication.py"))
        namespace = rehearsal["main"].__globals__
        namespace.update(FABRIC=root, PROJECTS_DIR=projects)
        real_commit = namespace["_commit"]

        def observe_commit(destination, *args):
            status = real_commit(destination, *args)
            observed["files"] = set(_git(destination, "ls-files", "--", "wiki", "evidence").splitlines())
            observed["outputs"] = _publication_outputs(destination)
            observed["claims"] = [
                json.loads(line) for line in (destination / "evidence/claims.jsonl").read_text().splitlines()
            ]
            return status

        monkeypatch.setitem(namespace, "_commit", observe_commit)
        monkeypatch.setattr(
            sys, "argv", ["dryrun_publication.py", str(run), str(prepared), "--branch", "agent/workflow"]
        )
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert rehearsal["main"]() == 0
        # Characterize real differences; do not normalize missing provenance or fields away.
        assert observed["claims"] == [
            dict(row, claim_type="", english_witness=None, witnesses_consulted=[]) for row in ledger
        ]
        assert production_files - observed["files"] == {"evidence/packet-execution.json"}
        assert observed["files"] - production_files == {"evidence/accepted-packets/worker-book-1.json"}
        shared = production_files & observed["files"] - {"evidence/claims.jsonl"}
        assert {name: observed["outputs"][name] for name in shared} == {
            name: production_outputs[name] for name in shared
        }
        assert _tree(run) == original_run
        assert (tmp_path / "calls.json").read_bytes() == native_calls
        assert _git(prepared, "rev-parse", "HEAD") == prepared_head
        assert _git(prepared, "status", "--porcelain") == ""
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
    assert set(identity["execution"]["code"]) == {"execution.py"}


def test_native_exhausted_shared_budget_never_accepts_optional_fallback(kb, model, tmp_path):
    from research_fabric.execution import configure, history, resolve

    run = tmp_path / "run"
    configure(run, resolve(override={"budget": {"requests": 1}}, native_model="openai/B", environ={}))
    with pytest.raises((CompilationError, RuntimeError)):
        native_compile(kb, [source(tmp_path)], execution_root=run)
    records = history(run)["attempts"]
    assert len(records) == 1
    assert not (kb / "wiki/concepts/one.md").exists()


def test_native_oauth_route_is_passed_to_existing_native_transport(kb, model, tmp_path, monkeypatch):
    import time

    from litellm.llms.chatgpt.authenticator import Authenticator

    from research_fabric.execution import configure, history, resolve

    run = tmp_path / "run"
    # Native provider detection reads OAuth state before the completion boundary.
    # Isolate it from developer credentials and never initiate device login.
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "auth.json").write_text(json.dumps({"access_token": "offline-fixture", "expires_at": time.time() + 3600}))
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tokens))
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "auth.json")

    def unexpected_login(self):
        pytest.fail("offline OAuth fixture must never initiate device login")

    monkeypatch.setattr(Authenticator, "_login_device_code", unexpected_login)
    configure(run, resolve(native_model="chatgpt/gpt-5", environ={}))
    report = native_compile(kb, [source(tmp_path)], execution_root=run)
    assert report["sources"]
    records = history(run)["attempts"]
    assert all(row["profile"]["auth"] == "oauth" for row in records)
    assert all(row["profile"]["provider"] == "chatgpt" for row in records)
    assert all(row["actual_model"] is None for row in records)
    assert set(model["models"]) == {"chatgpt/gpt-5"}


@pytest.fixture
def engine_run(kb, tmp_path, monkeypatch):
    """Production engine with only model I/O scripted; all gates and Git remain real."""
    import hashlib
    import subprocess
    import sys

    from research_fabric.engine import RunConfig

    root = Path(__file__).resolve().parents[1]
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-m", "baseline"),
        ("switch", "-c", "agent/engine"),
    ):
        subprocess.run(["git", "-C", str(kb), *args], check=True, capture_output=True)
    sources, run = tmp_path / "sources", tmp_path / "run"
    sources.mkdir()
    run.mkdir()
    rows = []
    for book in (1, 2):
        path = sources / f"odyssey-book-{book}.html"
        path.write_text(f"<p>Book {book}: Athena spoke to Telemachus.</p>")
        rows.append(
            {
                "source_id": f"s-odyssey-{book}-theoi",
                "snapshot": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "url": "https://example.invalid/source",
                "title": f"Synthetic {book}",
                "retrieved_at": "2026-01-01",
                "content_type": "text/html",
            }
        )
    (run / "source-manifest.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    fault = tmp_path / "fault"
    fault.write_text("")
    worker = tmp_path / "worker-python"
    worker.write_text(
        f"#!{sys.executable}\n"
        + f"fault_path={str(fault)!r}\n"
        + """
import json, pathlib, runpy, sys
import httpx
import openai
original = openai.OpenAI
filename = sys.argv[5]
def respond(request):
    body = json.loads(request.content)
    fault = pathlib.Path(fault_path).read_text()
    with pathlib.Path(fault_path).with_name("worker-calls.jsonl").open("a") as out:
        out.write(json.dumps({"file": filename, "model": body["model"]}) + "\\n")
    if fault == "worker" or (fault == "resume" and "book-2" in filename and body["model"] == "B"):
        return httpx.Response(503, json={"error": {"message": "scripted provider failure"}})
    excerpt = "Invented ungrounded quotation" if fault == "grounding" else "Athena spoke to Telemachus."
    packet = {"claims": [{"claim": f"Athena speaks, observation {i}", "source_file": filename,
                         "locator": "Book", "excerpt": excerpt, "stance": "supports", "confidence": 0.9}
                        for i in range(5)], "conflicts": [], "coverage_notes": []}
    return httpx.Response(200, json={"id": "scripted", "object": "chat.completion", "created": 1,
        "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(packet)}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
def client(**kwargs):
    return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
openai.OpenAI = client
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    )
    worker.chmod(0o755)
    lint = tmp_path / "offline-openkb"
    lint.write_text(
        f"#!{sys.executable}\n"
        + """
from openkb import cli
from openkb.agent import linter
async def advisory(*args, **kwargs):
    return "Offline advisory"
linter.run_knowledge_lint = advisory
cli.cli()
"""
    )
    lint.chmod(0o755)
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture")
    config = RunConfig(
        root,
        root / "projects/odyssey.yaml",
        kb,
        run,
        sources,
        "What happened?",
        worker_python=str(worker),
        openkb_command=(str(lint),),
        compiler_command=(
            sys.executable,
            str(root / "tests/fixtures/scripted_openkb.py"),
            str(tmp_path / "compiler-calls.json"),
        ),
        execution={"roles": {"extraction": {"model": "B"}}},
    )
    return config, fault


@pytest.mark.parametrize(
    "fault,stage",
    [
        ("worker", "RESEARCHING"),
        ("compile", "COMPILING"),
        ("grounding", "MATERIALIZING_LEDGER"),
        ("commit", "VERIFYING_DIFF"),
    ],
)
def test_engine_failure_never_publishes_ready(engine_run, fault, stage):
    import subprocess
    import sys
    from dataclasses import replace

    from research_fabric.engine import ResearchRun

    config, fault_path = engine_run
    fault_path.write_text(fault)
    if fault == "compile":
        script = fault_path.with_name("failed-compiler.py")
        script.write_text(
            "import pathlib,runpy,sys\npathlib.Path(sys.argv[1]).unlink(missing_ok=True)\n"
            + f"runpy.run_path({config.compiler_command[1]!r},run_name='__main__')\n"
        )
        config = replace(config, compiler_command=(sys.executable, str(script), config.compiler_command[2]))
    if fault == "commit":
        hook = config.field_root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    before = subprocess.check_output(["git", "-C", str(config.field_root), "rev-parse", "main"])
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        ResearchRun(config, lambda **_: "VERDICT: FAIL - advisory fixture").execute()
    state = json.loads((config.run_root / "run.json").read_text())
    assert state["state"] == "FAILED" and state["failed_stage"] == stage
    assert state["artifacts"] == str(config.run_root.resolve())
    assert "commit" not in state
    for ref in ("HEAD", "main"):
        assert subprocess.check_output(["git", "-C", str(config.field_root), "rev-parse", ref]) == before
    if fault == "compile":
        assert len(list((config.run_root / "verification/compile").glob("*/failure-*.json"))) == 2
        assert not (config.field_root / "wiki/concepts/one.md").exists()
    if fault == "grounding":
        assert "excerpt not found" in (config.run_root / "verification/excerpt-grounding.txt").read_text()


def test_engine_resume_switch_preserves_completed_packet_and_shared_budget(engine_run):
    import subprocess
    from dataclasses import replace

    from research_fabric.engine import ResearchRun
    from research_fabric.execution import history

    config, fault = engine_run
    fault.write_text("resume")
    config = replace(config, execution={"roles": {"extraction": {"model": "B"}}, "budget": {"requests": 4}})
    with pytest.raises(RuntimeError, match="evidence workers failed"):
        ResearchRun(config, lambda **_: "{}").execute()
    accepted_path = config.run_root / "evidence/worker-book-1.json"
    original_packet = json.loads(accepted_path.read_text())
    attempts = history(config.run_root)["attempts"]
    assert len(attempts) == 4
    assert subprocess.check_output(["git", "-C", str(config.field_root), "status", "--porcelain"]) == b""
    # Switching profiles does not replenish the exhausted run-wide budget.
    config = replace(
        config, reuse_evidence_dir=config.run_root / "evidence", execution={"roles": {"extraction": {"model": "C"}}}
    )
    with pytest.raises(RuntimeError, match="evidence workers failed"):
        ResearchRun(config, lambda **_: "{}").execute()
    assert history(config.run_root)["attempts"] == attempts
    # The operator explicitly increases the limit; prior attempts remain charged.
    config = replace(config, execution={"budget": {"requests": 100}})
    result = ResearchRun(config, lambda **_: "VERDICT: FAIL - advisory fixture").execute()
    assert result["state"] == "READY_FOR_REVIEW"
    current = history(config.run_root)
    assert current["attempts"][:4] == attempts
    extracted = [row for row in current["attempts"] if row["role"] == "extraction"]
    assert extracted[-1]["profile"]["model"] == "C" and extracted[-1]["revision"] == 3
    assert json.loads(accepted_path.read_text()) == original_packet
    calls = [json.loads(line) for line in fault.with_name("worker-calls.jsonl").read_text().splitlines()]
    assert len([call for call in calls if "book-1" in call["file"]]) == 1
    published = json.loads((config.field_root / "evidence/packet-execution.json").read_text())
    assert published["book-1"]["execution"] == original_packet["execution"]
    assert published["book-2"]["execution"][-1]["profile"]["model"] == "C"
    assert (config.run_root / "verification/provenance.txt").is_file()
    assert (config.run_root / "verification/excerpt-grounding.txt").is_file()
    assert subprocess.check_output(["git", "-C", str(config.field_root), "status", "--porcelain"]) == b""
