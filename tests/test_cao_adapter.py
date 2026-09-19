"""CAO maps launch inputs and reports the same engine result, without orchestrating."""

import ast
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from research_fabric import engine

ROOT = Path(__file__).resolve().parents[1]


def test_cao_maps_existing_inputs_and_reports_engine_result(tmp_path, monkeypatch):
    inputs = {
        "field_root": str(tmp_path / "kb"),
        "run_root": str(tmp_path / "run"),
        "source_dir": str(tmp_path / "sources"),
        "question": "Research",
        "reuse_evidence_dir": str(tmp_path / "old/evidence"),
        "project": "aeneid",
        "execution": '{"roles":{"extraction":{"model":"configured"}}}',
        "visual_preflight_spec": str(tmp_path / "visual.json"),
        "visual_python": "visual-python",
    }
    shim = ModuleType("cao_workflow")
    shim.get_inputs = lambda: inputs
    calls, outputs = [], []
    shim.run_step = lambda **kwargs: calls.append(kwargs) or SimpleNamespace(output={"plan": "recorded"})
    shim.emit_output = outputs.append
    monkeypatch.setitem(sys.modules, "cao_workflow", shim)
    monkeypatch.setenv("RESEARCH_FABRIC_ROOT", str(ROOT))
    monkeypatch.setenv("RESEARCH_FABRIC_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("RESEARCH_FABRIC_WORKER_PYTHON", "worker-python")
    result = {"state": "READY_FOR_REVIEW", "commit": "already-verified", "evidence": "evidence-path"}

    class CapturedRun:
        def __init__(self, config, agent_step, *, agent_provider):
            assert config.engine_root == ROOT
            assert config.project_path == tmp_path / "projects/aeneid.yaml"
            assert config.field_root == tmp_path / "kb"
            assert config.run_root == tmp_path / "run"
            assert config.source_dir == tmp_path / "sources"
            assert config.reuse_evidence_dir == tmp_path / "old/evidence"
            assert config.execution == json.loads(inputs["execution"])
            assert config.worker_python == "worker-python"
            assert config.visual_python == "visual-python"
            assert config.visual_preflight_spec == tmp_path / "visual.json"
            assert agent_provider == "claude_code"
            assert agent_step(
                agent="research-supervisor", step_id="plan", prompt="Research", timeout=300
            ) == json.dumps({"plan": "recorded"})

        def execute(self):
            return result

    monkeypatch.setattr(engine, "ResearchRun", CapturedRun)
    runpy.run_path(str(ROOT / "workflows/research.py"), run_name="__main__")
    assert calls == [
        {
            "provider": "claude_code",
            "agent": "research-supervisor",
            "step_id": "plan",
            "prompt": "Research",
            "timeout": 300,
        }
    ]
    assert outputs == [result]


def test_cao_python_script_launch_and_static_discovery(tmp_path):
    # CAO v2.4.1 script_runner._drive_process uses [sys.executable, script_path];
    # workflow_spec_service._extract_inputs reads the module-level dict via AST.
    script = tmp_path / "snapshot.py"
    script.write_text((ROOT / "workflows/research.py").read_text())
    tree = ast.parse(script.read_text())
    inputs = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "INPUTS" for target in node.targets)
    )
    assert "engine_root" in ast.literal_eval(inputs)
    (tmp_path / "cao_workflow.py").write_text(
        f"def get_inputs(): return dict(engine_root={str(ROOT)!r}, "
        f"field_root={str(tmp_path)!r}, run_root={str(tmp_path / 'run')!r}, "
        "source_dir='/source', question='q')\n"
        "def run_step(**kwargs): raise AssertionError('unexpected advisory call')\n"
        "def emit_output(result): raise AssertionError('invalid run published')\n"
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": str(Path(sys.executable).parent),
        "CAO_WORKFLOW_RUN_ID": "test",
        "CAO_WORKFLOW_GENERATION": "2",
        "CAO_API_BASE_URL": "http://unused",
        "CAO_WORKFLOW_INPUTS": "{}",
        "CAO_WORKFLOW_RESUME": "1",
    }
    (tmp_path / "run").mkdir()
    (tmp_path / "run/run.json").write_text('{"state": "READY_FOR_REVIEW", "commit": "old"}')
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "run artifacts must live outside the KB worktree" in result.stderr
    assert not result.stdout

    record = json.loads((tmp_path / "run/run.json").read_text())
    assert record == {"state": "READY_FOR_REVIEW", "commit": "old"}
