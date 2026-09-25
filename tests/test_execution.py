"""Real OpenAI client + worker scripts; replace only external HTTP responses."""

import json
import runpy
import sys
from pathlib import Path

import pytest

try:
    import httpx2 as httpx
except ImportError:
    import httpx

from research_fabric import execution as ex

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "sources" / "odyssey-book-1.html"
    path.parent.mkdir()
    path.write_text("<p>Homer: Athena spoke to Telemachus.</p>")
    return path


@pytest.fixture
def transport(monkeypatch):
    calls, replies = [], []
    original = ex.OpenAI

    def handler(request):
        calls.append(json.loads(request.content))
        status, content, model, usage = replies.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "SECRET body data:image/png;base64,PRIVATE"}})
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": usage,
            },
        )

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setenv("OPENROUTER_API_KEY", "SECRET")
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET")
    monkeypatch.setattr(ex, "OpenAI", client)
    return calls, replies


def configuration(**overrides):
    return ex.resolve(override=overrides, environ={})


def test_precedence_roles_and_settings():
    project = {"execution": {"roles": {"extraction": {"model": "project"}, "repair": {"model": "repair"}}}}
    config = ex.resolve(
        project,
        {"roles": {"extraction": {"model": "gpt-5", "effort": "high"}}},
        {"RESEARCH_FABRIC_WORKER_MODEL": "environment"},
    )
    assert config["roles"]["extraction"]["model"] == "gpt-5"
    assert config["roles"]["repair"]["model"] == "repair"
    for override in (
        {"roles": {"repair": {"effort": "high"}}},
        {"roles": {"extraction": {"auth": "oauth"}}},
        {"roles": {"extraction": {"api_key": "SECRET"}}},
    ):
        with pytest.raises(ex.ExecutionError):
            configuration(**override)
    with pytest.raises(ex.ExecutionError, match="project_model"):
        ex.resolve({"project": "aeneid"}, {"roles": {"extraction": {"model": "banned"}}}, {})


def test_retry_fallback_repair_and_shared_budget(tmp_path, source, transport):
    calls, replies = transport
    run = tmp_path / "run"
    config = configuration(
        roles={"extraction": {"model": "B"}, "repair": {"model": "R"}},
        fallbacks={"extraction": [{"model": "C"}]},
        budget={"requests": 3},
    )
    ex.configure(run, config)
    replies.extend(
        [
            (503, None, None, None),
            (200, "good", "C-returned", None),
            (200, "repaired", "R-returned", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}),
        ]
    )
    session = ex.Session(run, "extraction", "book-1", [source])
    assert session.call([{"role": "user", "content": "PRIVATE"}]) == "good"
    assert ex.Session(run, "repair", "claim-1", [source]).call([]) == "repaired"
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        session.call([])
    assert [c["model"] for c in calls] == ["B", "C", "R"]
    history = ex.history(run)
    assert [r["outcome"] for r in history["attempts"]] == ["transient", "accepted", "accepted"]
    assert history["unknown_usage_calls"] == 2
    assert "SECRET" not in json.dumps(history) and "PRIVATE" not in json.dumps(history)
    assert history["attempts"][1]["actual_model"] == "C-returned"


def test_authentication_never_falls_back(tmp_path, source, transport):
    calls, replies = transport
    ex.configure(tmp_path, configuration(fallbacks={"extraction": [{"provider": "openai", "model": "C"}]}))
    replies.append((401, None, None, None))
    with pytest.raises(ex.ExecutionError, match="authentication"):
        ex.Session(tmp_path, "extraction", "book-1", [source]).call([])
    assert len(calls) == 1


def test_resume_preserves_history_and_stale_session_cannot_call(tmp_path, source, transport):
    calls, replies = transport
    first = configuration(roles={"extraction": {"model": "B"}})
    ex.configure(tmp_path, first)
    session = ex.Session(tmp_path, "extraction", "book-1", [source])
    replies.append((200, "accepted", "B", None))
    session.call([])
    artifact = session.records()
    second = ex.resolve(override={"roles": {"extraction": {"model": "C"}}}, environ={}, previous=first)
    assert ex.configure(tmp_path, second) == 2
    with pytest.raises(ex.ExecutionError, match="configuration_changed"):
        session.call([])
    replies.append((200, "new", "C", None))
    ex.Session(tmp_path, "extraction", "book-2", [source]).call([])
    assert artifact == ex.history(tmp_path)["attempts"][:1]
    assert [r["revision"] for r in ex.history(tmp_path)["attempts"]] == [1, 2]
    assert ex.resolve(environ={}, previous=second) == second


def test_inflight_switch_blocked_and_budget_not_reset(tmp_path, source):
    ex.configure(tmp_path, configuration(budget={"requests": 1}))
    session = ex.Session(tmp_path, "extraction", "book-1", [source])
    session.begin(session.profiles()[0])
    with pytest.raises(ex.ExecutionError, match="in_flight"):
        ex.configure(tmp_path, configuration())
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        session.begin(session.profiles()[0])


def test_real_direct_worker_invalid_response_and_effort(tmp_path, source, transport, monkeypatch):
    calls, replies = transport
    run = tmp_path / "run"
    ex.configure(run, configuration(roles={"extraction": {"model": "gpt-5", "effort": "low"}}))
    claim = {"claim": "Athena speaks", "source_file": source.name, "locator": "1", "excerpt": "Athena spoke"}
    replies.extend([(200, "not JSON", "gpt-5", None), (200, json.dumps({"claims": [claim]}), "gpt-5-returned", None)])
    monkeypatch.setattr(sys, "argv", ["direct_worker.py", str(run), str(source.parent), "1", source.name])
    runpy.run_path(str(ROOT / "bin/direct_worker.py"), run_name="__main__")
    packet = json.loads((run / "evidence/worker-book-1.json").read_text())
    assert [r["outcome"] for r in packet["execution"]] == ["invalid_output", "accepted"]
    assert calls[0]["reasoning_effort"] == "low"
    assert packet["execution"][1]["actual_model"] == "gpt-5-returned"


def test_real_repair_uses_role_profile_and_shared_history(tmp_path, source, transport, monkeypatch):
    calls, replies = transport
    run = tmp_path / "run"
    (run / "evidence").mkdir(parents=True)
    ex.configure(run, configuration(roles={"repair": {"model": "repair-B"}}))
    packet = run / "evidence/worker-book-1.json"
    from research_fabric.claims import accept_packet, source_revision, stable_revision

    data = accept_packet(
        {
            "parsed": {
                "claims": [
                    {
                        "claim": "Athena speaks",
                        "excerpt": "wrong",
                        "source_file": source.name,
                        "locator": "1",
                        "stance": "supports",
                    }
                ]
            }
        },
        "book-1",
        source_dir=source.parent,
    )
    packet.write_text(json.dumps(data))
    metadata = {
        "version": 1,
        "source_revision": source_revision(source.parent),
        "packets": {"book-1": {key: data[key] for key in ("packet_revision", "packet_state_revision")}},
        "failures": [
            {"claim_id": "c-book-1-1", "worker": "book-1", "old_excerpt": "wrong", "reason": "excerpt not found"}
        ],
    }
    metadata["report_id"] = stable_revision(metadata)
    report = run / "report.txt"
    report.write_text("REPORT-META: " + json.dumps(metadata))
    replies.append((200, json.dumps({"found": True, "excerpt": "Athena spoke"}), "repair-actual", None))
    monkeypatch.syspath_prepend(str(ROOT / "bin"))
    repair = runpy.run_path(str(ROOT / "bin/repair_claims.py"))
    result = repair["repair_claims"](run, source.parent, report, acceptance={"min_claims": 1})
    assert result["repaired"] == 1
    assert calls[0]["model"] == "repair-B"
    accepted = json.loads(packet.read_text())
    assert accepted["execution"][0]["actual_model"] == "repair-actual"
    assert accepted["claim_history"][-1]["claim_id"] == "c-book-1-1"
    assert accepted["parsed"]["claims"][0]["excerpt"] == "Athena spoke"
    repair["repair_claims"](run, source.parent, report, acceptance={"min_claims": 1})
    assert len(calls) == 1


def test_known_token_limit_rejects_response_and_records_usage(tmp_path, source, transport):
    calls, replies = transport
    ex.configure(tmp_path, configuration(budget={"tokens": 4}))
    replies.append((200, "too expensive", "B", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}))
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        ex.Session(tmp_path, "extraction", "book-1", [source]).call([])
    row = ex.history(tmp_path)["attempts"][0]
    assert row["outcome"] == "budget_exhausted"
    assert row["usage"]["total_tokens"] == 5
    assert len(calls) == 1


def test_cross_process_completions_cannot_both_accept_over_budget(tmp_path, source):
    import os
    import subprocess

    ex.configure(tmp_path, configuration(budget={"tokens": 10}))
    session = ex.Session(tmp_path, "extraction", "book-1", [source])
    ids = [session.begin(session.profiles()[0])[0] for _ in range(2)]
    code = """import sys
from types import SimpleNamespace
from research_fabric.execution import Session, ExecutionError
s = Session(sys.argv[1], "extraction", "book-1", [sys.argv[2]])
r = SimpleNamespace(model="B", usage=SimpleNamespace(total_tokens=7))
try:
    s.finish(int(sys.argv[3]), r, "accepted", 0.01)
except ExecutionError:
    sys.exit(4)
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(source), str(i)], env=env) for i in ids
    ]
    assert sorted(p.wait(timeout=20) for p in processes) == [0, 4]
    rows = ex.history(tmp_path)["attempts"]
    assert sorted(row["outcome"] for row in rows) == ["accepted", "budget_exhausted"]
    assert sum(row["usage"]["total_tokens"] for row in rows) == 14


def test_source_attempt_identity_preserves_duplicate_basenames(tmp_path, source):
    other = tmp_path / "other" / source.name
    other.parent.mkdir()
    other.write_text("different bytes")
    ex.configure(tmp_path, configuration())
    first = ex.Session(tmp_path, "extraction", "book-1", [source, other]).source
    assert first == ex.Session(tmp_path, "extraction", "book-1", [other, source]).source
    assert first != ex.Session(tmp_path, "extraction", "book-1", [other]).source


def test_fixed_credentials_same_profile_dont_poison_new_candidate(tmp_path, source):
    from research_fabric.compilation import _check_retry_allowed

    config = ex.resolve(native_model="openai/B", environ={})
    ex.configure(tmp_path, config)
    session = ex.Session(tmp_path, "compile", "native-compile", [source])
    first, _ = session.begin(session.profiles()[0])
    session.finish(first, None, "authentication", 0.01)
    with pytest.raises(RuntimeError, match="operator"):
        _check_retry_allowed(tmp_path, 0)
    checkpoint = first
    assert ex.configure(tmp_path, config) == 1
    second, _ = session.begin(session.profiles()[0])
    session.finish(second, None, "transient", 0.01)
    _check_retry_allowed(tmp_path, checkpoint)
    assert len(ex.history(tmp_path)["attempts"]) == 2


def test_pending_duration_reservation_and_timeout_accounting(tmp_path, source):
    ex.configure(tmp_path, configuration(budget={"seconds": 1, "attempt_seconds": 1}))
    session = ex.Session(tmp_path, "extraction", "book-1", [source])
    attempt, timeout = session.begin(session.profiles()[0])
    assert timeout == 1
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        session.begin(session.profiles()[0])
    with pytest.raises(ex.ExecutionError, match="budget_exhausted"):
        session.finish(attempt, None, "accepted", 1.1)
    assert ex.history(tmp_path)["attempts"][0]["outcome"] == "budget_exhausted"


def test_cross_process_request_reservations_obey_shared_limit(tmp_path, source):
    import os
    import subprocess

    ex.configure(tmp_path, configuration(budget={"requests": 1}))
    code = """import sys
from research_fabric.execution import Session, ExecutionError
s = Session(sys.argv[1], "extraction", "book-1", [sys.argv[2]])
try:
    s.begin(s.profiles()[0])
except ExecutionError:
    sys.exit(4)
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(source)], env=env) for _ in range(2)]
    assert sorted(p.wait(timeout=20) for p in processes) == [0, 4]
    assert len(ex.history(tmp_path)["attempts"]) == 1


@pytest.mark.parametrize("provider", ["openai", "openrouter", "nvidia"])
def test_native_api_route_cannot_claim_oauth(provider):
    with pytest.raises(ex.ExecutionError, match="provider_auth"):
        ex.resolve(
            override={"roles": {"compile": {"provider": provider, "auth": "oauth"}}},
            native_model="openai/B",
            environ={},
        )


def test_unqualified_astra_api_effort_is_explicit_error():
    with pytest.raises(ex.ExecutionError, match="unsupported_reasoning_effort"):
        ex.resolve(override={"roles": {"compile": {"effort": "medium"}}}, native_model="openai/gpt-6-astra", environ={})


@pytest.mark.parametrize("existing", [False, True])
def test_standalone_aeneid_rejects_banned_profile_before_calls(tmp_path, source, transport, monkeypatch, existing):
    calls, _ = transport
    run = tmp_path / "run"
    if existing:
        ex.configure(run, configuration(roles={"extraction": {"model": "stealth/ox-alpha"}}))
    monkeypatch.setenv("RESEARCH_FABRIC_WORKER_MODEL", "stealth/ox-alpha")
    monkeypatch.setattr(sys, "argv", ["aeneid_worker.py", str(run), str(source.parent), "1", source.name])
    with pytest.raises(ex.ExecutionError, match="project_model_restriction"):
        runpy.run_path(str(ROOT / "bin/aeneid_worker.py"), run_name="__main__")
    assert calls == []
    assert not (run / "evidence/worker-book-1.json").exists()


@pytest.mark.parametrize("actual,valid", [(None, True), ("allowed", True), ("forbidden", False)])
def test_artifact_policy_keeps_unknown_returned_identity_honest(actual, valid):
    row = {"role": "extraction", "outcome": "accepted", "profile": {"model": "allowed"}, "actual_model": actual}
    packet = {
        "execution": [
            row,
            {"role": "advisory", "outcome": "accepted", "actual_model": "other"},
            {"role": "extraction", "outcome": "transient", "profile": {"model": "other"}},
        ]
    }
    assert bool(ex.packet_policy_defects(packet, {"allowed_models": ["allowed"]})) is not valid


def test_restricted_artifact_requires_original_extraction_history():
    policy = {"allowed_models": ["allowed"]}
    assert ex.packet_policy_defects({}, policy)
    assert ex.packet_policy_defects(
        {"execution": [{"role": "repair", "outcome": "accepted", "profile": {"model": "allowed"}}]}, policy
    )
    assert ex.packet_policy_defects({}, {}) == []


@pytest.mark.parametrize(
    "case",
    [
        "second-witness",
        "one-witness",
        "invented-selection",
        "missing-selection",
        "invalid-index",
        "no-latin",
        "no-witness",
        "json-retry",
    ],
)
def test_real_aeneid_stage_grounding_and_fallback(tmp_path, transport, monkeypatch, case):
    """Only HTTP is scripted: adapters, stage assembly and attempt accounting run."""
    calls, replies = transport
    run, sources = tmp_path / "run", tmp_path / "sources"
    sources.mkdir()
    latin = sources / "latin.html"
    latin.write_text("<p>Arma virumque cano</p>")
    (sources / "kline.html").write_text("<p>Arms and the man I sing</p>")
    (sources / "mackail.html").write_text("<p>I sing of arms and a man</p>")
    claim = {
        "claim": "The poet sings",
        "excerpt": "wrong Latin" if case == "no-latin" else "Arma virumque cano",
        "locator": "Aen.1.1",
        "source_file": latin.name,
    }
    values = [{"claims": [claim]}]
    if case == "json-retry":
        replies.append((200, "[invalid JSON", "z-ai/glm-5.3-flash", None))
    if case != "no-latin":
        for text in (
            ("Arms and the man I sing",)
            if case == "one-witness"
            else ("Arms and the man I sing", "I sing of arms and a man")
        ):
            values.append({"selections": [] if case == "no-witness" else [{"i": "1", "excerpt": text}]})
    if case not in ("no-latin", "no-witness"):
        selection = {
            "i": "bad" if case == "invalid-index" else 1,
            "translator": "Kline" if case == "one-witness" else "Mackail",
            "excerpt": "Arms and the man I sing" if case == "one-witness" else "I sing of arms and a man",
        }
        if case == "invented-selection":
            selection["excerpt"] = "Model invented this rendering"
        values.append({"selections": [] if case == "missing-selection" else [selection]})
    replies.extend((200, json.dumps(value), "z-ai/glm-5.3-flash", None) for value in values)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aeneid_worker.py",
            str(run),
            str(sources),
            "1",
            "latin.html",
            "theme",
            "--witness",
            "Kline:s-kline:kline.html",
        ]
        + ([] if case == "one-witness" else ["--witness", "Mackail:s-mackail:mackail.html"]),
    )
    output = run / "evidence/worker-book-1.json"
    if case in ("no-latin", "no-witness"):
        with pytest.raises(RuntimeError, match="no verbatim Latin|no claims with a verbatim English"):
            runpy.run_path(str(ROOT / "bin/aeneid_worker.py"), run_name="__main__")
        assert not output.exists()
        assert len(calls) == (1 if case == "no-latin" else 3)
        return
    runpy.run_path(str(ROOT / "bin/aeneid_worker.py"), run_name="__main__")
    packet = json.loads(output.read_text())
    row = packet["parsed"]["claims"][0]
    chosen = "Mackail" if case in ("second-witness", "json-retry") else "Kline"
    assert row["english_witness"]["translator"] == chosen
    assert row["english_witness"]["excerpt"] in (sources / f"{chosen.lower()}.html").read_text()
    witnesses = {"s-kline"} if case == "one-witness" else {"s-kline", "s-mackail"}
    assert set(row["witnesses_consulted"]) == witnesses
    assert len(row["witnesses_consulted"]) == len(witnesses)
    assert len(packet["source_provenance"]) == len(witnesses) + 1
    assert len(calls) == len(packet["execution"]) == len(witnesses) + 2 + (case == "json-retry")
    assert all(row["actual_model"] == "z-ai/glm-5.3-flash" for row in packet["execution"])
    if case == "json-retry":
        assert packet["execution"][0]["outcome"] == "invalid_output"


@pytest.mark.parametrize(
    "allowed", ["not-allowed-model", {"allowed": True}, 0, False, [None], [""], ["  "], [["allowed"]]]
)
@pytest.mark.parametrize("project_name", ["other", "aeneid"])
def test_malformed_model_restriction_rejects_profiles_and_historical_artifacts(allowed, project_name):
    project = {"project": project_name, "allowed_models": allowed}
    row = {"role": "extraction", "outcome": "accepted", "profile": {"model": "allowed"}, "actual_model": "allowed"}
    with pytest.raises(ex.ExecutionError, match="allowed_models_requires_list|invalid_identity"):
        ex.resolve(project, {"roles": {"extraction": {"model": "allowed"}, "repair": {"model": "allowed"}}}, environ={})
    with pytest.raises(ex.ExecutionError, match="allowed_models_requires_list|invalid_identity"):
        ex.packet_policy_defects({"execution": [row]}, project)


@pytest.mark.parametrize(
    "allowed,accepted", [(None, True), ([], False), (["allowed"], True), (["not-allowed-model"], False)]
)
def test_model_restriction_exact_membership_and_empty_semantics(allowed, accepted):
    project = {"allowed_models": allowed}
    override = {"roles": {"extraction": {"model": "allowed"}, "repair": {"model": "allowed"}}}
    row = {"role": "extraction", "outcome": "accepted", "profile": {"model": "allowed"}, "actual_model": "allowed"}
    if accepted:
        ex.resolve(project, override, environ={})
    else:
        with pytest.raises(ex.ExecutionError, match="project_model_restriction"):
            ex.resolve(project, override, environ={})
    assert bool(ex.packet_policy_defects({"execution": [row]}, project)) is not accepted


def test_completion_is_one_time_and_preserves_original_record(tmp_path, source):
    ex.configure(tmp_path, configuration())
    session = ex.Session(tmp_path, "extraction", "book-1", [source])
    attempt, _ = session.begin(session.profiles()[0])
    session.finish(attempt, None, "transient", 0.1)
    recorded = ex.history(tmp_path)
    with pytest.raises(ex.ExecutionError, match="already_completed"):
        session.finish(attempt, None, "accepted", 0.2)
    assert ex.history(tmp_path) == recorded


def test_legacy_journal_is_rejected_without_reset_or_mutation(tmp_path):
    import sqlite3

    path = tmp_path / "execution.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE revisions(id INTEGER); CREATE TABLE starts(id INTEGER);"
            "CREATE TABLE outcomes(id INTEGER); INSERT INTO starts VALUES(7);"
        )
    original = path.read_bytes()
    with pytest.raises(ex.ExecutionError, match="unsupported_execution_journal"):
        ex.configure(tmp_path, configuration())
    assert path.read_bytes() == original
