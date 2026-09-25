"""Replay external replies through native inspection and production reading dispatch."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("openkb")

import httpx
from test_derived_context import NOTE, reading_run
from test_visual_preflight_native import call, message, replay  # noqa: F401

from research_fabric._source_assets import publish_bundle
from research_fabric.claims import accept_packet, atomic_write_json
from research_fabric.derived_context import context_path, prepare_contexts
from research_fabric.engine import _reuse_evidence_packets
from research_fabric.publication import publish_candidate
from research_fabric.run_state import _publication_outputs


def _worker_replies(monkeypatch):
    requests = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-only")

    def reply(client, request, **kwargs):
        body = json.loads(request.content)
        requests.append(body)
        content = body["messages"][0]["content"]
        visual = "PRIMARY SOURCE a/paper.md" in content
        excerpt = "The relation is illustrated below." if visual else "Measurements require calibration."
        parsed = {
            "claims": [
                {
                    "source_file": "a/paper.md" if visual else "b/paper.md",
                    "excerpt": excerpt,
                    "claim": excerpt,
                    "locator": "L3",
                    "stance": "supports",
                    "confidence": 0.8,
                }
            ],
            "coverage_notes": ["Exponent requires independent review."] if visual else [],
            "conflicts": [],
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(parsed)},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    monkeypatch.setattr(httpx.Client, "send", reply)
    return requests


def _cli_dispatch(monkeypatch):
    # Execute the real CLI in-process only to retain external endpoint replay patches.
    # No preparation, native query, SDK request serialization or worker code is replaced.
    native_run = subprocess.run

    def dispatch(command, **kwargs):
        if len(command) > 1 and Path(command[1]).name in {"visual_preflight.py", "direct_worker.py"}:
            with monkeypatch.context() as scoped:
                scoped.setattr(sys, "argv", command[1:])
                runpy.run_path(command[1], run_name="__main__")
            return subprocess.CompletedProcess(command, 0, "offline CLI executed", "")
        return native_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", dispatch)
    return native_run


def _publish(run, texts, native_run):
    # Prepared compiler output is a fixture; publication and all its gates execute.
    kb = run.config.field_root
    (kb / "wiki/summaries").mkdir(parents=True)
    for args in (
        ("init", "-b", "agent/visual"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("commit", "--allow-empty", "-m", "baseline"),
    ):
        native_run(["git", "-C", str(kb), *args], check=True, capture_output=True)
    for bundle in run.bundles.values():
        doc = Path(bundle["input_path"]).stem
        for asset in bundle["assets"]:
            image = kb / "wiki/sources/images" / doc / Path(asset["derivative_path"]).name
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes((run.compiler_dir / bundle["key"] / asset["derivative_path"]).read_bytes())
        publish_bundle(run.compiler_dir / bundle["key"], bundle, kb / "wiki", doc)
        (kb / "wiki/summaries" / f"{doc}.md").write_text("# Prepared summary\n")
    return publish_candidate(
        field_root=kb,
        run_root=run.config.run_root,
        engine_root=run.config.engine_root,
        source_dir=run.config.source_dir,
        source_files=run.source_files,
        manifest_rows=run.manifest_rows,
        worker_ids=[row["id"] for row in run.reading_plan.data["readings"]],
        notes={},
        sources={name: name for name in texts},
        compiled={
            "state": "COMPLETE",
            "sources": [{"summary": Path(b["input_path"]).stem, "required": []} for b in run.bundles.values()],
            "outputs": _publication_outputs(kb),
        },
        message="offline derived context publication",
        reading_plan=run.reading_plan,
    )


def test_inspection_reaches_real_reading_workers_and_publication(tmp_path, replay, monkeypatch):  # noqa: F811
    run, spec, texts = reading_run(tmp_path)
    spec["assets"].append({"path": "unassigned/equation.png", "sha256": "0" * 64, "source_locator": "unassigned"})
    atomic_write_json(run.config.visual_preflight_spec, spec)
    outputs, inspections = replay
    outputs.extend([[call()], [message(NOTE)]])
    requests = _worker_replies(monkeypatch)
    native_run = _cli_dispatch(monkeypatch)
    frozen = (run.config.run_root / "reading-plan.json").read_bytes()
    run.plan()
    run._collect()
    assert len(inspections) == 2 and len(requests) == 2
    assert any(item["type"] == "function_call_output" for item in inspections[1]["input"])
    assert NOTE in requests[0]["messages"][0]["content"]
    assert "NOT verbatim source evidence" in requests[0]["messages"][0]["content"]
    assert NOTE not in requests[1]["messages"][0]["content"]
    assert (run.config.run_root / "reading-plan.json").read_bytes() == frozen
    assert all((run.config.source_dir / name).read_text() == text for name, text in texts.items())
    first, second = [row["id"] for row in run.reading_plan.data["readings"]]
    with pytest.raises(ValueError, match="missing"):
        run.reading_plan.claim(first, {"source_file": "a/paper.md", "excerpt": "E = m c^2"})
    packet = json.loads((run.packet_dir / f"worker-{first}.json").read_text())
    claim = packet["parsed"]["claims"][0]
    assert claim["claim_id"] == f"c-{first}-1"
    assert run.reading_plan.project_claim(claim)["independence_group"] == "A"
    baseline = copy.deepcopy(packet)
    baseline.pop("derived_context")
    assert (
        accept_packet(baseline, first, source_dir=run.config.source_dir)["packet_revision"] == packet["packet_revision"]
    )
    run.plan()  # Same captured note, no additional native calls.
    assert len(inspections) == 2
    assert _reuse_evidence_packets(
        run.packet_dir,
        tmp_path / "reused",
        run.worker_specs,
        run.worker_provenance,
        lambda parsed: [],
        run.config.source_dir,
        policy=run._packet_policy,
    ) == [first, second]
    changed = copy.deepcopy(spec)
    changed["instructions"] += " New instruction."
    outputs.extend([[call()], [message(NOTE + " Reconsidered.")]])
    atomic_write_json(run.config.visual_preflight_spec, changed)
    with pytest.raises(ValueError, match="derived context drift"):
        run.plan()
    # Retain historical context and reject reuse under the new context.
    old = run.derived_contexts[first]
    new = copy.deepcopy(old)
    new["assets"][0]["inspection"]["note"] += " Changed."
    run.derived_contexts[first] = new
    assert _reuse_evidence_packets(
        run.packet_dir,
        tmp_path / "changed-reuse",
        run.worker_specs,
        run.worker_provenance,
        lambda parsed: [],
        run.config.source_dir,
        policy=run._packet_policy,
    ) == [second]
    run.derived_contexts[first] = old
    # Shared/context figures consume the same exact note, not a basename match.
    shared_run, _, _ = reading_run(tmp_path / "shared", shared=True)
    shared = prepare_contexts(
        shared_run.reading_plan,
        shared_run.bundles,
        shared_run.config.run_root,
        spec,
        lambda single, key: old["assets"][0]["inspection"],
    )
    assert all(NOTE in json.dumps(context) for context in shared.values())
    result = _publish(run, texts, native_run)
    kb = run.config.field_root
    published = json.loads((kb / "evidence/packet-execution.json").read_text())
    assert published[first]["derived_context"] == old == result["review"]["derived_context"][first]
    assert result["review"]["derived_review"][first] == ["Exponent requires independent review."]
    assert not old["compiler_received"] and not old["assets"][0]["inspection"]["assets"][0]["reviewed"]
    assert result["review"]["mechanical"]["ok"]
    assert native_run(["git", "-C", str(kb), "status", "--porcelain"], capture_output=True).stdout == b""
    context_path(run.config.run_root, first).unlink()
    from research_fabric._publication_inputs import _accepted_packets

    with pytest.raises(FileNotFoundError):
        _accepted_packets(run.config.run_root, [first], run.config.source_dir, run.reading_plan, {})


@pytest.mark.parametrize("required", [False, True])
def test_unresolved_native_inspection_is_explicit_or_blocks(tmp_path, replay, monkeypatch, required):  # noqa: F811
    run, _, _ = reading_run(tmp_path)
    run.project["reading"]["visual_context"] = {"required": required}
    outputs, _ = replay
    outputs.append([message("I cannot establish the exponent without image access.")])
    _cli_dispatch(monkeypatch)
    if required:
        with pytest.raises(ValueError, match="required inspection unresolved"):
            run.plan()
    else:
        run.plan()
        first = run.reading_plan.data["readings"][0]["id"]
        asset = run.derived_contexts[first]["assets"][0]
        assert asset["limitation"] == asset["inspection"]["unresolved"]
        assert not asset["inspection"]["assets"][0]["inspected"]
