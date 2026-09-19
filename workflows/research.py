"""CAO launch/reporting adapter for the importable research engine."""

import json
import os
import pathlib
import sys

INPUTS = {
    "engine_root": {"type": "path", "required": False},
    "field_root": {"type": "path", "required": True},
    "run_root": {"type": "path", "required": True},
    "source_dir": {"type": "path", "required": True},
    "question": {"type": "string", "required": True},
    "reuse_evidence_dir": {"type": "path", "required": False},
    "project": {"type": "string", "required": False},
    "execution": {"type": "string", "required": False},
    "visual_preflight_spec": {"type": "path", "required": False},
    "visual_python": {"type": "path", "required": False},
}


def configuration(inputs, environ):
    root = pathlib.Path(inputs.get("engine_root") or environ.get("RESEARCH_FABRIC_ROOT", pathlib.Path(__file__).resolve().parents[1])).resolve()
    sys.path.insert(0, str(root / "src"))
    from research_fabric.engine import RunConfig

    projects = pathlib.Path(environ.get("RESEARCH_FABRIC_PROJECTS", root / "projects"))
    return RunConfig(
        engine_root=root,
        project_path=(projects / f"{inputs.get('project') or 'odyssey'}.yaml").resolve(),
        field_root=pathlib.Path(inputs["field_root"]).resolve(),
        run_root=pathlib.Path(inputs["run_root"]).resolve(),
        source_dir=pathlib.Path(inputs["source_dir"]).resolve(),
        question=inputs["question"],
        reuse_evidence_dir=pathlib.Path(inputs["reuse_evidence_dir"]).resolve()
        if inputs.get("reuse_evidence_dir")
        else None,
        execution=json.loads(inputs.get("execution") or "{}"),
        visual_preflight_spec=pathlib.Path(inputs["visual_preflight_spec"]).resolve()
        if inputs.get("visual_preflight_spec")
        else None,
        visual_python=inputs.get("visual_python") or sys.executable,
        worker_python=environ.get("RESEARCH_FABRIC_WORKER_PYTHON", sys.executable),
    )


def main():
    from cao_workflow import emit_output, get_inputs, run_step

    def agent_step(**kwargs):
        output = run_step(provider="claude_code", **kwargs).output
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)

    config = configuration(get_inputs(), os.environ)
    from research_fabric.engine import ResearchRun

    result = ResearchRun(config, agent_step, agent_provider="claude_code").execute()
    emit_output(result)


if __name__ == "__main__":
    main()
