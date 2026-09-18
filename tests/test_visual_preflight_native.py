"""Offline external Responses replay through the installed converter and native runner."""

import asyncio
import base64
import copy
import json

import pytest

pytest.importorskip("openkb")

from research_fabric import _oauth_visual as adapter
from research_fabric.visual_preflight import preflight, preparation_note

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAE0lEQVR4nGP8//8/AwMDEwMYAAAkBgMBXaJOiAAAAABJRU5ErkJggg=="
)


def message(text="Final transcription x=1."):
    return {
        "id": "msg",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call(index=0, path=None, call_id=None):
    return {
        "id": f"fc_{index}",
        "type": "function_call",
        "call_id": call_id or f"original_{index}",
        "name": "get_image",
        "arguments": json.dumps({"image_path": path or f"sources/images/{index}.png"}),
    }


@pytest.fixture
def replay(monkeypatch, tmp_path):
    from litellm.types.llms.openai import ResponsesAPIResponse

    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(
        "litellm.llms.chatgpt.authenticator.Authenticator.get_access_token", lambda self: "offline-only"
    )
    # No SDK/provider network may escape the external response replay boundary.
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("unexpected network access"))
    outputs, requests = [], []

    async def external_response(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        assert kwargs["model"] == "gpt-6-astra"
        assert kwargs["reasoning"] == {"effort": "medium"}
        assert "Configured inspection instruction." in kwargs["instructions"]
        assert kwargs.get("stream") is not True
        payload = outputs.pop(0)
        response_fields = {"output": payload} if isinstance(payload, list) else payload
        return ResponsesAPIResponse(
            id=f"response_{len(requests)}",
            created_at=1,
            model="gpt-6-astra",
            object="response",
            parallel_tool_calls=True,
            tool_choice="auto",
            tools=[],
            temperature=1,
            top_p=1,
            status=response_fields.pop("status", "completed"),
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            **response_fields,
        )

    monkeypatch.setattr("litellm.aresponses", external_response)
    return outputs, requests


def config():
    return {
        "model": "chatgpt/gpt-6-astra",
        "effort": "medium",
        "instructions": "Configured inspection instruction.",
        "question": "Inspect the images.",
    }


def native_query(tmp_path, outputs, count=2):
    wiki = tmp_path / "wiki"
    (wiki / "sources/images").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (wiki / f"sources/images/{index}.png").write_bytes(PNG)
    calls = []
    result = asyncio.run(adapter.query(wiki, config(), calls))
    return result, calls


@pytest.mark.parametrize("count", [1, 2])
def test_native_tools_preserve_ids_and_reach_next_turn(tmp_path, replay, count):
    outputs, requests = replay
    outputs.extend([[message("I will inspect."), *[call(i) for i in range(count)]], [message()]])
    answer, calls = native_query(tmp_path, outputs)
    assert answer == "Final transcription x=1."
    assert [c["call_id"] for c in calls] == [f"original_{i}" for i in range(count)]
    assert all(c["result"] == "image" for c in calls)
    next_input = requests[1]["input"]
    results = [item for item in next_input if item["type"] == "function_call_output"]
    assert {r["call_id"] for r in results} == {c["call_id"] for c in calls}
    assert all(r["output"][0]["type"] == "input_image" for r in results)
    emitted = [item for item in next_input if item["type"] == "function_call"]
    assert [item["arguments"] for item in emitted] == [c["arguments"] for c in calls]


def test_broken_installed_combination_drops_tool_calls(tmp_path, replay):
    from agents import ModelSettings, RunConfig, Runner
    from agents.extensions.models.litellm_model import LitellmModel
    from openai.types.shared import Reasoning
    from openkb.agent.query import build_query_agent

    outputs, requests = replay
    outputs.append([message("I will inspect."), call()])
    agent = build_query_agent(str(tmp_path), "chatgpt/responses/gpt-6-astra")
    agent.instructions += "\nConfigured inspection instruction."
    agent.model = LitellmModel(model="chatgpt/responses/gpt-6-astra")
    agent.model_settings = ModelSettings(
        reasoning=Reasoning(effort="medium"), extra_args={"allowed_openai_params": ["reasoning_effort"]}
    )
    result = asyncio.run(Runner.run(agent, "Inspect images", run_config=RunConfig(tracing_disabled=True)))
    assert result.final_output == "I will inspect."
    assert len(requests) == 1
    assert not any(item.type == "tool_call_output_item" for item in result.new_items)


@pytest.mark.parametrize(
    "first", [[message()], [call()], [{"id": "reason", "type": "reasoning", "summary": []}, call()]]
)
def test_supported_shapes(tmp_path, replay, first):
    outputs, requests = replay
    outputs.extend([first, [message()]])
    answer, calls = native_query(tmp_path, outputs)
    assert answer == "Final transcription x=1."
    assert len(calls) == (0 if first[0]["type"] == "message" else 1)


@pytest.mark.parametrize("retry", [False, True])
def test_duplicate_ids_never_execute_twice(tmp_path, replay, retry):
    outputs, requests = replay
    outputs.extend([[call()], [call()], [message()]] if retry else [[call(), call()]])
    with pytest.raises(Exception, match="duplicate/missing call ID"):
        native_query(tmp_path, outputs)
    assert len(requests) == (2 if retry else 1)


def test_genuine_alternatives_rejected_without_merging(tmp_path, replay, monkeypatch):
    from litellm.types.utils import ModelResponse

    async def alternatives(**kwargs):
        return ModelResponse(
            choices=[{"index": i, "message": {"role": "assistant", "content": str(i)}} for i in range(2)]
        )

    monkeypatch.setattr("litellm.acompletion", alternatives)
    with pytest.raises(ValueError, match="alternative choices"):
        native_query(tmp_path, [])


def spec(tmp_path):
    from research_fabric.visual_preflight import digest

    (tmp_path / "equation.png").write_bytes(PNG)
    (tmp_path / "chapter.md").write_text("Original Markdown without a transcription.")
    return {k: v for k, v in config().items() if k != "question"} | {
        "assets": [{"path": "equation.png", "sha256": digest(PNG), "source_locator": "chapter.md#equation"}]
    }


def test_preflight_trace_cache_and_derived_boundary(tmp_path, replay):
    outputs, requests = replay
    original = spec(tmp_path)
    outputs.extend([[message("Inspect."), call()], [message()]])
    record = preflight(tmp_path, original, tmp_path / "record.json")
    assert record["unresolved"] is None
    assert record["assets"][0]["inspected"] and not record["assets"][0]["reviewed"]
    assert not record["assets"][0]["rendered"]  # raster supplied; no renderer ran
    assert record["calls"][0]["result_sha256"] == record["assets"][0]["sha256"]
    assert "NOT verbatim" in preparation_note(record)
    assert "x=1" not in (tmp_path / "chapter.md").read_text()
    assert not (tmp_path / "wiki").exists()
    assert preflight(tmp_path, original, tmp_path / "record.json") == record
    assert len(requests) == 2
    assert "data:image" not in (tmp_path / "record.json").read_text()


@pytest.mark.parametrize("first", [[message()], [call(path="missing.png")], [call(), call()]])
def test_incomplete_preflight_remains_unresolved(tmp_path, replay, first):
    outputs, _ = replay
    outputs.extend([first, [message()]])
    record = preflight(tmp_path, spec(tmp_path), tmp_path / "record.json")
    assert record["unresolved"]
    assert not record["assets"][0]["inspected"]
    assert not record["assets"][0]["reviewed"]


def test_versions_and_unsupported_shapes_fail_explicitly(monkeypatch):
    monkeypatch.setattr(adapter, "version", lambda _: "unexpected")
    with pytest.raises(RuntimeError, match="rerun compatibility tests"):
        adapter.supported_versions()
    with pytest.raises(ValueError, match="unsupported Responses"):
        adapter._validate_item({"type": "unknown"}, set())


@pytest.mark.parametrize(
    "envelope",
    [
        {"status": "incomplete", "output": [message(), call()]},
        {"status": "failed", "output": [message(), call()]},
        {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}, "output": [message(), call()]},
        {"status": "completed", "output": [message(), call() | {"status": "in_progress"}]},
    ],
)
def test_incomplete_envelopes_never_execute_tools(tmp_path, replay, envelope):
    outputs, requests = replay
    outputs.append(copy.deepcopy(envelope))
    record = preflight(tmp_path, spec(tmp_path), tmp_path / "record.json")
    assert record["unresolved"] and not record["calls"]
    assert not record["assets"][0]["inspected"]
    assert len(requests) == 1


def test_cached_record_and_asset_drift_rejected(tmp_path, replay):
    outputs, _ = replay
    original = spec(tmp_path)
    outputs.extend([[call()], [message()]])
    output = tmp_path / "record.json"
    record = preflight(tmp_path, original, output)
    record["request"]["config"]["effort"] = "high"
    output.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="checksum"):
        preflight(tmp_path, original, output)
    from research_fabric.visual_preflight import digest

    record.pop("record_sha256")
    record["record_sha256"] = digest(json.dumps(record, sort_keys=True).encode())
    output.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="different inputs"):
        preflight(tmp_path, original, output)
    (tmp_path / "equation.png").write_bytes(b"drift")
    with pytest.raises(ValueError, match="hash mismatch"):
        preflight(tmp_path, original, output)


def test_derived_transcription_fails_original_quote_gate(tmp_path, replay):
    import runpy
    from pathlib import Path

    outputs, _ = replay
    outputs.extend([[call()], [message()]])
    record = preflight(tmp_path, spec(tmp_path), tmp_path / "record.json")
    gate = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/excerpt_grounding.py"))
    assert not gate["grounded"](record["note"], (tmp_path / "chapter.md").read_text())


@pytest.mark.skipif(
    __import__("os").environ.get("RESEARCH_VISUAL_LIVE_SMOKE") != "1",
    reason="explicit OAuth account opt-in only; never in offline CI",
)
def test_live_oauth_smoke(tmp_path):
    import os

    original = spec(tmp_path)
    original["model"] = os.environ["RESEARCH_VISUAL_MODEL"]
    record = preflight(tmp_path, original, tmp_path / "record.json")
    assert record["unresolved"] is None
    assert record["assets"][0]["inspected"]


def test_workflow_preparation_uses_record_without_changing_sources(tmp_path, replay):
    """Exercise actual workflow planning statements; replay only process and supervisor boundaries."""
    import ast
    import pathlib
    import sys
    from types import SimpleNamespace

    outputs, _ = replay
    outputs.extend([[call()], [message()]])
    original = spec(tmp_path)
    spec_path = tmp_path / "visual.json"
    spec_path.write_text(json.dumps(original))
    root = pathlib.Path(__file__).resolve().parents[1]
    module = ast.parse((root / "workflows/research.py").read_text())
    lifecycle = next(
        node
        for node in module.body
        if isinstance(node, ast.With)
        and isinstance(node.items[0].context_expr, ast.Call)
        and isinstance(node.items[0].context_expr.func, ast.Name)
        and node.items[0].context_expr.func.id == "run_lifecycle"
    )
    nodes = lifecycle.body
    start = next(
        i
        for i, n in enumerate(nodes)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "visual_preparation" for t in n.targets)
    )
    captured = []

    def child(command, **kwargs):
        assert command[1] == str(root / "bin/visual_preflight.py")
        preflight(command[3], json.loads(pathlib.Path(command[5]).read_text()), command[7])

    def supervisor(**kwargs):
        captured.append(kwargs["prompt"])
        return SimpleNamespace(output="{}")

    namespace = {
        "inputs": {"visual_preflight_spec": str(spec_path)},
        "pathlib": pathlib,
        "sys": sys,
        "subprocess": SimpleNamespace(run=child),
        "RESEARCH_ROOT": root,
        "run_root": tmp_path / "run",
        "source_dir": tmp_path,
        "json": json,
        "reuse_evidence_dir": None,
        "question": "Research",
        "source_files": [tmp_path / "chapter.md"],
        "run_step": supervisor,
        "output_text": lambda h: h.output,
        "extract_json": json.loads,
        "write_json": lambda *args: None,
    }
    exec(compile(ast.Module(body=nodes[start : start + 3], type_ignores=[]), "workflow-planning", "exec"), namespace)
    assert "DERIVED VISUAL NOTES" in captured[0] and "NOT verbatim source evidence" in captured[0]
    assert "chapter.md#equation" in captured[0]
    assert (tmp_path / "chapter.md").read_text() == "Original Markdown without a transcription."


@pytest.mark.parametrize("change", [{"model": "openai/gpt-6-astra"}, {"stream": True}, {"effort": "unknown"}])
def test_no_api_fallback_or_unsupported_configuration(tmp_path, change):
    original = spec(tmp_path) | change
    with pytest.raises(ValueError):
        preflight(tmp_path, original, tmp_path / "record.json")
    assert not (tmp_path / "record.json").exists()


def test_native_tool_exception_records_unresolved(tmp_path, replay, monkeypatch):
    def fail_read(*args):
        raise OSError("synthetic failure containing private payload, do not persist")

    monkeypatch.setattr("openkb.agent.query.read_wiki_image", fail_read)
    outputs, _ = replay
    outputs.extend([[call()], [message()]])
    record = preflight(tmp_path, spec(tmp_path), tmp_path / "record.json")
    assert record["unresolved"] and record["calls"][0]["result"] == "error"
    assert not record["assets"][0]["inspected"]
    assert "private payload" not in json.dumps(record)
