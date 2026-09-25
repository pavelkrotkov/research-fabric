"""Offline HTTP replay through native OAuth and real worker/compiler entrypoints."""

import json
import runpy
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("openkb")

from research_fabric import execution as ex
from research_fabric._oauth_execution import prepare
from research_fabric.claims import accept_packet, source_revision, stable_revision

ROOT = Path(__file__).resolve().parents[1]


def profile(model="gpt-6-astra", effort="medium"):
    return {"provider": "chatgpt", "auth": "oauth", "model": model, "effort": effort}


def configuration(**extra):
    return ex.resolve(
        override={
            "subscription_only": True,
            "roles": dict.fromkeys(("extraction", "repair", "compile"), profile()),
            **extra,
        },
        environ={},
    )


def envelope(text, model="gpt-6-astra", **extra):
    return {
        "id": "offline-response",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [
            {
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        **extra,
    }


@pytest.fixture
def wire(monkeypatch, tmp_path):
    import httpx

    auth = tmp_path / "tokens"
    auth.mkdir()
    (auth / "auth.json").write_text(
        json.dumps({"access_token": "SYNTHETIC-SECRET", "account_id": "PRIVATE", "expires_at": time.time() + 3600})
    )
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(auth))
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "auth.json")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    for key in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "CHATGPT_API_BASE",
        "OPENAI_CHATGPT_API_BASE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ex, "_client", lambda *a, **k: pytest.fail("API route called"))
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("unexpected network"))
    replies, requests = [], []

    def send(client, request, **kwargs):
        assert str(request.url) in (
            "https://chatgpt.com/backend-api/codex/responses",
            "https://auth.openai.com/oauth/token",
        )
        requests.append(json.loads(request.content))
        result = replies.pop(0)
        if callable(result):
            result = result(requests[-1])
        if isinstance(result, tuple):
            return httpx.Response(result[0], json=result[1], request=request)
        event = {"type": "response." + result["status"], "response": result}
        return httpx.Response(
            200,
            text="data: " + json.dumps(event) + "\n\n",
            request=request,
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx.Client, "send", send)
    return replies, requests, auth / "auth.json"


def repair_report(run, sources, data):
    metadata = {
        "version": 1,
        "source_revision": source_revision(sources),
        "packets": {"book-1": {key: data[key] for key in ("packet_revision", "packet_state_revision")}},
        "failures": [
            {"claim_id": "c-book-1-1", "worker": "book-1", "old_excerpt": "wrong", "reason": "excerpt not found"}
        ],
    }
    metadata["report_id"] = stable_revision(metadata)
    report = run / "report.txt"
    report.write_text("REPORT-META: " + json.dumps(metadata))
    return report


def test_real_entrypoints_failure_switch_reuse_and_repair(tmp_path, monkeypatch, wire):
    from research_fabric.engine import _reuse_evidence_packets
    from research_fabric.sources import source_provenance

    replies, requests, _ = wire
    sources, run = tmp_path / "sources", tmp_path / "run"
    sources.mkdir()
    source = sources / "book-1.html"
    source.write_text("<p>Athena spoke to Telemachus.</p>")
    initial = configuration(budget={"requests": 4})
    ex.configure(run, initial)
    claim = {
        "claim": "Athena speaks",
        "source_file": source.name,
        "locator": "1",
        "excerpt": "wrong",
        "stance": "supports",
    }
    replies.extend([envelope("not JSON"), envelope(json.dumps({"claims": [claim]})), (429, {"error": "PRIVATE"})])
    monkeypatch.setattr(sys, "argv", ["direct_worker.py", str(run), str(sources), "1", source.name])
    runpy.run_path(str(ROOT / "bin/direct_worker.py"), run_name="__main__")
    packet = run / "evidence/worker-book-1.json"
    accepted = accept_packet(json.loads(packet.read_text()), "book-1", source_dir=sources)
    packet.write_text(json.dumps(accepted))
    with pytest.raises(ex.ExecutionError, match="subscription_quota"):
        ex.Session(run, "extraction", "book-2", [source]).call([])
    updated = ex.resolve(
        previous=initial, override={"roles": {"extraction": profile("gpt-5"), "repair": profile("gpt-5")}}, environ={}
    )
    ex.configure(run, updated)
    assert _reuse_evidence_packets(
        run / "evidence",
        run / "evidence",
        [("book-1", "")],
        {"book-1": source_provenance([source])},
        lambda parsed: [],
        sources,
    ) == ["book-1"]
    assert json.loads(packet.read_text())["execution"] == accepted["execution"]
    replies.append(envelope(json.dumps({"found": True, "excerpt": "Athena spoke"}), "gpt-5", usage=None))
    report = repair_report(run, sources, accepted)
    monkeypatch.syspath_prepend(str(ROOT / "bin"))
    repair = runpy.run_path(str(ROOT / "bin/repair_claims.py"))["repair_claims"]
    assert repair(run, sources, report, acceptance={"min_claims": 1})["repaired"] == 1
    result = json.loads(packet.read_text())
    assert result["claim_history"][-1]["claim_id"] == "c-book-1-1"
    assert result["execution"][:2] == accepted["execution"]
    rows = ex.history(run)["attempts"]
    assert [r["outcome"] for r in rows] == ["invalid_output", "accepted", "subscription_quota", "accepted"]
    assert [r["revision"] for r in rows] == [1, 1, 1, 2]
    assert sum(r["usage"]["total_tokens"] or 0 for r in rows) == 14
    assert ex.history(run)["unknown_usage_calls"] == 2
    assert [r["model"] for r in requests] == ["gpt-6-astra"] * 3 + ["gpt-5"]
    assert all(r["reasoning"] == {"effort": "medium"} for r in requests)
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        ex.Session(run, "repair", "again", [source]).call([])
    assert "PRIVATE" not in json.dumps(ex.history(run)) and "SYNTHETIC-SECRET" not in json.dumps(ex.history(run))


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "credential_unavailable"),
        ("expired", "credential_unavailable"),
        ("refresh", "authentication_refresh_failed"),
        (401, "authentication"),
        (403, "authentication"),
        (429, "subscription_quota"),
        (503, "attempt_limit"),
        ("truncated", "attempt_limit"),
    ],
)
def test_failures_are_bounded_sanitized_and_never_login(tmp_path, wire, case, reason):
    replies, requests, auth = wire
    if case == "missing":
        auth.unlink()
    elif case in ("expired", "refresh"):
        auth.write_text(
            json.dumps(
                {"access_token": "old", "expires_at": 1, **({"refresh_token": "PRIVATE"} if case == "refresh" else {})}
            )
        )
        if case == "refresh":
            replies.append((401, {"error": "PRIVATE"}))
    elif case == "truncated":
        replies.append(envelope("partial", status="incomplete", incomplete_details={"reason": "max_output_tokens"}))
    else:
        replies.append((case, {"error": "PRIVATE"}))
    ex.configure(tmp_path / "run", configuration(budget={"attempts": 1}))
    with pytest.raises(ex.ExecutionError, match=reason):
        ex.Session(tmp_path / "run", "extraction", "test", []).call([{"role": "user", "content": "hello"}])
    history = ex.history(tmp_path / "run")
    assert len(history["attempts"]) == 1
    assert len(requests) <= 1
    assert "PRIVATE" not in json.dumps(history)
    if case == "truncated":
        assert history["attempts"][0]["usage"]["total_tokens"] == 7


@pytest.mark.parametrize("model", ["gpt-5", "gpt-6-astra"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_settings_and_full_body(tmp_path, wire, model, effort):
    replies, requests, _ = wire
    config = configuration(roles=dict.fromkeys(("extraction", "repair", "compile"), profile(model, effort)))
    ex.configure(tmp_path / "run", config)
    text = "line\n" * 350
    replies.append(envelope(text, model))
    assert ex.Session(tmp_path / "run", "extraction", "test", []).call([{"role": "user", "content": "hello"}]) == text
    assert requests[0].get("reasoning") == ({"effort": effort} if effort else None)
    assert requests[0]["model"] == model
    assert "temperature" not in requests[0]


def test_refresh_uses_native_store_and_pending_attempt_blocks_resume(tmp_path, wire):
    replies, requests, auth = wire
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=")
    auth.write_text(json.dumps({"access_token": "old", "expires_at": 1, "refresh_token": "PRIVATE"}))
    replies.extend([(200, {"access_token": "x." + payload + ".x", "id_token": "synthetic-id"}), envelope("ok")])
    run = tmp_path / "run"
    ex.configure(run, configuration())
    session = ex.Session(run, "extraction", "test", [])
    assert session.call([{"role": "user", "content": "hello"}]) == "ok"
    assert requests[0]["grant_type"] == "refresh_token"
    session.begin(session.profiles()[0])
    with pytest.raises(ex.ExecutionError, match="in_flight"):
        ex.configure(run, configuration())
    assert len(ex.history(run)["attempts"]) == 2


def test_unqualified_settings_and_api_fallback_rejected(wire):
    for override in ({"model": "unknown"}, {"effort": "xhigh"}, {"account": "other"}):
        with pytest.raises(ex.ExecutionError):
            configuration(roles={"extraction": {**profile(), **override}, "repair": profile()})
    with pytest.raises(ex.ExecutionError, match="subscription_only"):
        configuration(fallbacks={"extraction": [{"provider": "openai", "model": "gpt-5"}]})
    assert wire[1] == []


def test_endpoint_override_rejected(wire, monkeypatch):
    monkeypatch.setenv("CHATGPT_API_BASE", "https://api.openai.com/v1")
    with pytest.raises(ex.ExecutionError, match="unsupported_oauth_endpoint"):
        prepare()
    assert wire[1] == []


@pytest.mark.parametrize("model", ["gpt-5", "gpt-6-astra"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_real_native_compile_uses_same_oauth_wire(tmp_path, wire, model, effort):
    from test_native_compilation import kb, source

    from research_fabric._openkb045 import native_compile

    root = kb.__wrapped__(tmp_path)
    replies, requests, _ = wire
    config = configuration(roles=dict.fromkeys(("extraction", "repair", "compile"), profile(model, effort)))
    run = tmp_path / "run"
    ex.configure(run, config)

    def reply(request):
        prompt = json.dumps(request["input"])
        if "decide how to update" in prompt:
            text = json.dumps({"create": [{"name": "one"}]})
        elif "Rewrite the summary" in prompt:
            text = "Final summary"
        else:
            text = json.dumps({"description": "Synthetic source", "content": "Native generated body"})
        return envelope(text, model)

    replies.extend([reply] * 8)
    report = native_compile(root, [source(tmp_path)], execution_root=run)
    assert report["sources"]
    assert (root / "wiki/concepts/one.md").is_file()
    assert all(r["model"] == model and r.get("reasoning") == ({"effort": effort} if effort else None) for r in requests)
    rows = ex.history(run)["attempts"]
    assert len(rows) == len(requests) and all(r["role"] == "compile" and r["outcome"] == "accepted" for r in rows)


def test_subscription_only_disables_optional_calls(tmp_path, monkeypatch):
    from research_fabric.engine import ResearchRun, RunConfig

    run = ResearchRun(
        RunConfig(
            tmp_path, tmp_path / "project.yaml", tmp_path / "kb", tmp_path / "run", tmp_path / "sources", "question"
        ),
        lambda **kwargs: pytest.fail("unqualified advisory route"),
    )
    run.subscription_only = True
    run.config.run_root.mkdir()
    (run.config.run_root / "verification").mkdir()
    run.plan()
    assert json.loads((run.config.run_root / "plan.json").read_text())["status"] == "unavailable"
    run._advisory("verify", "prompt", "reply.txt", "advisory.json")
    assert json.loads((run.config.run_root / "verification/advisory.json").read_text())["verdict"] is None
    result = run._advisory_review({"candidate_sha256": "abc", "changed": []})
    assert result["status"] == "unavailable" and result["verdict"] is None
    run.source_files = []
    monkeypatch.setattr("research_fabric.engine.compile_with_recovery", lambda *a, **k: {})
    monkeypatch.setattr("research_fabric.engine.normalize_generated_log", lambda *a: None)
    monkeypatch.setattr("research_fabric.engine.subprocess.run", lambda *a, **k: pytest.fail("unqualified lint"))
    run._compile()


def test_reported_effort_and_usage_survive_rejected_response(tmp_path, wire):
    replies, requests, _ = wire
    run = tmp_path / "run"
    ex.configure(run, configuration(budget={"attempts": 1}))
    replies.append(envelope("answer", reasoning={"effort": "low"}))
    with pytest.raises(ex.ExecutionError, match="attempt_limit"):
        ex.Session(run, "repair", "claim-1", []).call([])
    row = ex.history(run)["attempts"][0]
    assert row["settings"] == {
        "sent_effort": "medium",
        "reported_effort": "low",
        "output_limit": "acceptance_only",
        "temperature": "provider_default",
    }
    assert row["usage"]["total_tokens"] == 7


@pytest.mark.parametrize(
    "code,reason", [("usage_limit_reached", "subscription_quota"), ("invalid_token", "authentication")]
)
def test_http_success_with_failed_sse_does_not_fall_back(tmp_path, wire, code, reason):
    replies, requests, _ = wire
    ex.configure(tmp_path, configuration(fallbacks={"extraction": [profile("gpt-5")]}))
    replies.append(envelope("", status="failed", error={"code": code, "message": "PRIVATE"}))
    with pytest.raises(ex.ExecutionError, match=reason):
        ex.Session(tmp_path, "extraction", "test", []).call([])
    assert len(requests) == 1
    assert ex.history(tmp_path)["attempts"][0]["usage"]["total_tokens"] == 7
