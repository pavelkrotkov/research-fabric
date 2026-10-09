"""Pre-dispatch bounds and source-unit identity (native I/O tested end-to-end)."""

import copy
import json

import pytest

from research_fabric.claims import stable_revision
from research_fabric.compilation import CompilationError
from research_fabric.compile_units import prepare_units
from research_fabric.execution import configure, history, resolve
from research_fabric.reading import prepare_reading_plan
from research_fabric.sources import representation_for, source_attestation


def prepared(tmp_path, text, *, requests=100, policy_text="", reviewed=None):
    pytest.importorskip("openkb")
    source = tmp_path / "sources/book.md"
    source.parent.mkdir()
    source.write_text(text)
    root = tmp_path / "kb"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki/AGENTS.md").write_text(policy_text)
    row = {**source_attestation(representation_for(source), source.parent), "source_id": "book"}
    project = {
        "reading": {
            "sources": {"book.md": "work"},
            "works": {"work": {"title": None, "authors": None}},
            "policy": {"target_tokens": 12},
        }
    }
    if reviewed:
        (source.parent / "reviewed.json").write_text(json.dumps(reviewed))
        project["reading"]["reviewed_plan"] = "reviewed.json"
    plan, bundles = prepare_reading_plan(
        source.parent, project, [row], tmp_path / "reading-plan.json", tmp_path / "compiler-sources"
    )
    configure(tmp_path, resolve(override={"budget": {"requests": requests}}, native_model="openai/test", environ={}))
    return plan, bundles, project, root


@pytest.mark.parametrize("kind", ["block", "policy", "quota", "missing_context"])
def test_unresolved_before_dispatch(tmp_path, kind):
    plan, bundles, project, root = prepared(
        tmp_path,
        "# A\n\n" + ("long argument " * 5000 if kind == "block" else "Evidence.") + "\n",
        requests=1 if kind == "quota" else 100,
        policy_text="extra policy " * 4000 if kind == "policy" else "",
    )
    if kind == "missing_context":
        plan.data["readings"][0]["context"] = ["absent"]
        plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    with pytest.raises((CompilationError, ValueError, KeyError)):
        prepare_units(plan, bundles, project, tmp_path, root)
    assert history(tmp_path)["attempts"] == []
    if kind != "missing_context":
        report = json.loads((tmp_path / "compile-planning-report.json").read_text())
        assert report["unresolved"] if kind != "quota" else report["minimum_fresh_requests"] > 1


def test_reviewed_subdivision_freezes_coordinates_and_policy(tmp_path):
    reviewed = {
        "sections": {
            "a": {"source_file": "book.md", "lines": [1, 5], "disposition": "primary"},
            "b": {"source_file": "book.md", "lines": [5, 6], "disposition": "primary"},
        },
        "readings": [
            {"id": "a", "work_id": "work", "primary": ["a"]},
            {"id": "b", "work_id": "work", "primary": ["b"]},
        ],
    }
    plan, bundles, project, root = prepared(
        tmp_path, "# A\n\nFirst finding.\n\nSecond qualification.\n", reviewed=reviewed
    )
    frozen = prepare_units(plan, bundles, project, tmp_path, root)
    assert len(frozen["units"]) == 2
    first, second = frozen["units"]
    assert first["id"] != second["id"]
    assert first["primary"]["a"]["original_bytes"][1] == second["primary"]["b"]["original_bytes"][0]
    assert "Second qualification" not in (tmp_path / "compile-units" / first["input_path"]).read_text()
    assert '"section": "b"' not in first["native_policy"]
    assert prepare_units(plan, bundles, project, tmp_path, root) == frozen
    changed = copy.deepcopy(project)
    changed["compilation"] = {"mode": "stock"}
    with pytest.raises(CompilationError, match="drift"):
        prepare_units(plan, bundles, changed, tmp_path, root)


def test_native_total_envelope_rejected_before_transport(tmp_path, monkeypatch):
    pytest.importorskip("openkb")
    from openkb import cli
    from openkb.agent import compiler

    from research_fabric._openkb045 import native_execution
    from research_fabric.execution import ExecutionError

    source = tmp_path / "unit.md"
    source.write_text("small source")
    configure(tmp_path, resolve(native_model="openai/test", environ={}))
    (tmp_path / "compile-plan.json").write_text(json.dumps({"policy": {"max_request_tokens": 32000}}))
    monkeypatch.setattr(compiler.litellm, "completion", lambda **_: pytest.fail("oversized transport dispatched"))
    with pytest.raises(ExecutionError, match="envelope"), native_execution(tmp_path, [source], compiler, cli, 0):
        compiler.litellm.completion(messages=[{"role": "system", "content": "existing-page context " * 3000}])
    assert history(tmp_path)["attempts"] == []
