#!/usr/bin/env python3
"""Explicitly opt-in one-book launch; production engine and publisher remain authoritative."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_fabric.claims import atomic_write_json  # noqa: E402
from research_fabric.core import load_project  # noqa: E402
from research_fabric.engine import ResearchRun, RunConfig  # noqa: E402
from research_fabric.execution import resolve  # noqa: E402


def freeze_evaluation(source_dir, evaluation_path, run_root):
    """Bind predeclared expectations to actual original lines, before generation."""
    evaluation = json.loads(evaluation_path.read_text())
    if not evaluation.get("sampling") or not evaluation.get("visual_omissions") or not evaluation.get("items"):
        raise ValueError("evaluation requires sampling, visual_omissions and nonempty items")
    ids, sources = set(), {}
    for item in evaluation["items"]:
        if not item["id"] or item["id"] in ids:
            raise ValueError("evaluation IDs must be nonempty and unique")
        ids.add(item["id"])
        path = (source_dir / item["source_file"]).resolve()
        if not path.is_relative_to(source_dir.resolve()):
            raise ValueError("evaluation source escapes source directory")
        content = path.read_bytes()
        lines = content.decode("utf-8").splitlines()
        start, end = item["lines"]
        if not (type(start) is int and type(end) is int and 1 <= start < end <= len(lines) + 1):
            raise ValueError("evaluation requires one-based, end-exclusive original line spans")
        if not item["expected"] or item["expected"] not in "\n".join(lines[start - 1 : end - 1]):
            raise ValueError("evaluation expectation absent from original source span")
        sources[item["source_file"]] = hashlib.sha256(content).hexdigest()
    frozen = {"evaluation": evaluation, "source_sha256": sources}
    destination = run_root / "acceptance-evaluation.json"
    if destination.exists():
        if json.loads(destination.read_text()) != frozen:
            raise ValueError("evaluation drift: retain historical run; use a new run for a changed evaluation")
    else:
        if (run_root / "execution.sqlite3").exists() or (run_root / "run.json").exists():
            raise ValueError("cannot declare a new evaluation after generation started")
        atomic_write_json(destination, frozen)
    return frozen


def launch(config_path, *, resume=False):
    supplied = json.loads(config_path.read_text())
    required = {"project_path", "field_root", "run_root", "source_dir", "question", "evaluation_path", "execution"}
    if set(supplied) != required:
        raise ValueError(f"config must contain exactly {sorted(required)}")
    paths = {
        key: Path(supplied[key]).expanduser().resolve() for key in required if key.endswith(("_path", "_root", "_dir"))
    }
    if paths["run_root"].is_relative_to(paths["field_root"]):
        raise ValueError("private run artifacts must live outside the candidate KB")
    project = load_project(paths["project_path"].parent, paths["project_path"].stem)
    reading = project.get("reading", {})
    if len(reading.get("works", {})) != 1 or set(reading.get("sources", {}).values()) != set(reading["works"]):
        raise ValueError("this acceptance launcher requires exactly one explicit Work and its source mapping")
    execution = supplied["execution"]
    if execution.get("subscription_only") is not True:
        raise ValueError("one-book live acceptance requires subscription_only: true")
    for role in ("extraction", "repair", "compile"):
        profile = execution.get("roles", {}).get(role, {})
        if not {"provider", "auth", "account", "model", "effort"} <= profile.keys():
            raise ValueError(f"select the complete {role} route/model/effort explicitly")
    budget = {"requests", "seconds", "tokens", "attempt_seconds", "attempt_tokens", "output_tokens", "attempts"}
    if set(execution.get("budget", {})) != budget:
        raise ValueError("select every cumulative/per-call budget explicitly")
    if "policy" not in reading or "max_request_tokens" not in project.get("compilation", {}):
        raise ValueError("select reading policy and compilation.max_request_tokens before launch")
    resolve(project, execution)  # Reject unsupported routes/settings before creating run artifacts.
    frozen = freeze_evaluation(paths["source_dir"], paths["evaluation_path"], paths["run_root"])
    if set(frozen["source_sha256"]) != set(reading["sources"]):
        raise ValueError("evaluation must include source locators from every supplied chapter/snapshot")
    receipt = {
        "config": supplied,
        "python": platform.python_version(),
        "toolchain": {
            name: importlib.metadata.version(name)
            for name in ("research-fabric", "openkb", "litellm", "openai", "openai-agents", "tiktoken", "Pillow")
        },
        "decision": "NOT_READY",
        "reason": "Live execution, route audit and human semantic review have not all been accepted.",
    }
    # Append launch revisions; never overwrite a previous model/budget/toolchain selection.
    receipts = paths["run_root"] / "acceptance-launches.jsonl"
    with receipts.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt) + "\n")
    config = RunConfig(
        engine_root=Path(__file__).resolve().parents[1],
        project_path=paths["project_path"],
        field_root=paths["field_root"],
        run_root=paths["run_root"],
        source_dir=paths["source_dir"],
        question=supplied["question"],
        execution=execution,
        reuse_evidence_dir=paths["run_root"] / "evidence" if resume else None,
    )
    result = ResearchRun(config, None).execute()
    atomic_write_json(
        paths["run_root"] / "acceptance-status.json",
        {
            "decision": "NOT_READY",
            "engine_state": result["state"],
            "proposed_commit": result["commit"],
            "reason": "Mechanical publication is not semantic qualification. Complete the operator/browser audit.",
        },
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="private JSON configuration; see docs/one-book-acceptance.md")
    parser.add_argument("--live", action="store_true", help="authorize subscription model requests")
    parser.add_argument("--resume", action="store_true", help="reuse compatible evidence without resetting budgets")
    args = parser.parse_args()
    if not args.live:
        parser.error("no work performed: --live is required; offline acceptance uses pytest")
    result = launch(args.config, resume=args.resume)
    print(json.dumps({"state": result["state"], "commit": result["commit"], "decision": "NOT_READY"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
