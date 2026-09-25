"""Bounded reading inputs; OpenKB still plans and writes every wiki page.

One frozen reading is one native document, not one independent publication.
Completed prefixes live outside the user worktree; only the complete aggregate
is applied. Semantic coverage is review evidence, never an LLM acceptance gate.
"""

from __future__ import annotations

import difflib
import json
import pathlib
import shutil
import sys

from ._source_assets import _derived_text, _visual_source, publish_bundle, verify_bundle
from .claims import stable_revision
from .compilation import (
    CompilationError,
    _apply_candidate,
    _git,
    _tree,
    assert_run_branch,
    compile_with_recovery,
    digest,
    write_json,
)
from .execution import configured, history
from .sources import _reading_tokenizer

POLICY = "reading-native-units-v1"
COVERAGE_POLICY = """
## Research coverage and retention
Each native document is a bounded unit of an original Work, NOT an independent
source. Native source-note counts are not independent Work counts. Preserve
important concepts, mathematical assumptions, qualifications, uncertainty and
conflicts. Do not use an initial document/page-count cap as a coverage target:
retain every supported distinction in summaries or linked concept/entity pages.
Prefer updates to duplicates. When updating, retain earlier supported distinctions
and citations; qualify or contrast conflicting results instead of overwriting them.
Semantic completeness requires human review; source disposition is only mechanical.
"""


def _policy(project):
    policy = {"max_request_tokens": 32000, "mode": "coverage", **project.get("compilation", {})}
    if set(policy) != {"max_request_tokens", "mode"} or policy["mode"] not in {"coverage", "stock"}:
        raise ValueError("unsupported compilation policy")
    if type(policy["max_request_tokens"]) is not int or policy["max_request_tokens"] <= 0:
        raise ValueError("compilation max_request_tokens must be positive")
    return {"version": POLICY, **policy}


def _unit(plan, reading, bundles, directory, policy, base_policy):
    keys = [*reading["primary"], *reading["context"]]
    names = plan.source_names(reading["id"])
    assets = [{**ref, "record": bundles[ref["source_file"]]["assets"][ref["index"]]} for ref in reading["assets"]]
    unit = {
        "reading_id": reading["id"],
        "work_id": reading["work_id"],
        "primary": {key: plan.section(key) for key in reading["primary"]},
        "context": {key: plan.section(key) for key in reading["context"]},
        "sources": {name: plan.data["sources"][name] for name in names},
        "assets": assets,
        "derived_context": [],
        "policy": policy,
        "plan_sha256": plan.data["sha256"],
    }
    unit["id"] = "unit-" + stable_revision(unit)[:24]
    root = directory / unit["id"]
    root.mkdir(parents=True, exist_ok=True)
    text = f"# Compilation unit {unit['id']}\n\nParent Work: {reading['work_id']}\n" + plan.input(reading["id"])
    _, text = _visual_source(text)
    for name in names:
        bundle = bundles[name]
        records = [ref["record"] for ref in assets if ref["source_file"] == name]
        text = _derived_text(text, records, bundle["key"])
        for record in records:
            if "derivative_path" in record:
                relative = record["derivative_path"]
                target = root / relative.removeprefix("prepared/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(directory.parent / "compiler-sources" / bundle["key"] / relative, target)
    path = root / (unit["id"] + ".md")
    path.write_text(text, encoding="utf-8")
    unit["input_sha256"] = digest(path)
    unit["input_path"] = path.relative_to(directory).as_posix()
    unit["native_policy"] = base_policy + (COVERAGE_POLICY if policy["mode"] == "coverage" else "")
    unit["native_policy"] += plan.citation_policy(keys)
    return unit


def prepare_units(plan, bundles, project, run_root, field_root):
    """Freeze identities and emit a pre-call report, without increasing any quota."""
    from openkb.schema import get_agents_md

    from ._openkb045 import _native_modules

    plan.validate()
    policy = _policy(project)
    directory = run_root / "compile-units"
    base_policy = get_agents_md(field_root / "wiki").split("\n## Frozen research source citations\n")[0]
    for bundle in bundles.values():
        verify_bundle(run_root / "compiler-sources" / bundle["key"], bundle)
    units = [_unit(plan, row, bundles, directory, policy, base_policy) for row in plan.data["readings"]]
    _, compiler, versions = _native_modules()
    frozen = {
        "policy": policy,
        "reading_plan_sha256": plan.data["sha256"],
        "units": units,
        "toolchain": {"versions": versions, "compiler_sha256": digest(compiler.__file__)},
        "bundles": {name: bundle["key"] for name, bundle in bundles.items()},
    }
    frozen["sha256"] = stable_revision(frozen)
    path = run_root / "compile-plan.json"
    if path.exists() and json.loads(path.read_text()) != frozen:
        raise CompilationError("frozen compile plan/source/policy/asset drift")
    write_json(path, frozen)
    budget = configured(run_root)[1]["budget"]
    encoding, _ = _reading_tokenizer()
    sizes = []
    for unit in units:
        text = (directory / unit["input_path"]).read_text()
        envelope = text + unit["native_policy"]
        sizes.append(
            {
                "unit": unit["id"],
                "input_tokens": len(encoding.encode(text, disallowed_special=())),
                "policy_tokens": len(encoding.encode(unit["native_policy"], disallowed_special=())),
                "minimum_envelope_bytes": len(envelope.encode()) + budget["output_tokens"] + 1024,
            }
        )
    limit = min(policy["max_request_tokens"], budget["attempt_tokens"])
    unresolved = [row["unit"] for row in sizes if row["minimum_envelope_bytes"] > limit]
    # Native short-document flow requests summary, planner, and optional rewrite;
    # page writes and retries add unknown demand, never silently increase spend.
    minimum = len(plan.data["readings"]) + 3 * len(units)
    report = {
        "compile_plan_sha256": frozen["sha256"],
        "units": sizes,
        "unresolved": unresolved,
        "minimum_fresh_requests": minimum,
        "configured_requests": budget["requests"],
        "request_limit": limit,
        "independent_works": len(plan.data["works"]),
        "source_notes": len(units),
        "limitations": "Byte-conservative preflight is a lower envelope; native dynamic context is checked per call. "
        "Page generation, retries, external advisory and native lint are not included in minimum demand.",
    }
    write_json(run_root / "compile-planning-report.json", report)
    if unresolved:
        raise CompilationError(
            "unresolved oversized compile units; review syntax-safe reading subdivisions or dispositions"
        )
    if minimum > budget["requests"]:
        raise CompilationError("insufficient configured run request budget; see compile-planning-report.json")
    return frozen


def _session(kb, run_root, frozen, command):
    directory = run_root / "verification/compile/units"
    directory.mkdir(parents=True, exist_ok=True)
    stage = directory / "candidate"
    identity = {
        "plan": frozen["sha256"],
        "baseline": _tree(kb),
        "head": _git(kb, "rev-parse", "HEAD"),
        "command": list(command),
        "code": {
            name: digest(pathlib.Path(__file__).with_name(name))
            for name in ("compile_units.py", "compilation.py", "_openkb045.py", "execution.py")
        },
        "executable": digest(shutil.which(command[0]) or command[0]),
        "entrypoint": digest(command[1]) if len(command) > 1 and pathlib.Path(command[1]).is_file() else None,
    }
    checkpoint = directory / "checkpoint.json"
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
        if state["identity"] != identity or state["outputs"] != _tree(stage):
            raise CompilationError("compile checkpoint input/policy/toolchain/candidate drift")
    else:
        if stage.exists():
            raise CompilationError("interrupted unaccepted unit candidate; preserve diagnostics and use a new run")
        shutil.copytree(kb, stage, ignore=shutil.ignore_patterns(".git"))
        for args in (
            ("init", "-b", "agent/compile-units"),
            ("config", "user.email", "compile@local"),
            ("config", "user.name", "Compilation"),
            ("add", "-A"),
            ("commit", "-m", "Baseline"),
        ):
            _git(stage, *args)
        state = {"identity": identity, "outputs": _tree(stage), "sources": [], "retention": []}
        write_json(checkpoint, state)
    return directory, stage, checkpoint, state


def compile_units(kb, run_root, frozen, *, command=None):
    """Use the existing accepted-candidate seam per unit and atomically apply the aggregate."""
    from . import compilation

    assert_run_branch(kb)
    if json.loads((run_root / "compile-plan.json").read_text()) != frozen:
        raise CompilationError("compile plan drift before dispatch")
    command = tuple(command or (sys.executable, str(pathlib.Path(compilation.__file__).resolve())))
    directory, stage, checkpoint, state = _session(kb, run_root, frozen, command)
    policy_path = stage / "wiki/AGENTS.md"
    original = policy_path.read_bytes() if policy_path.exists() else None
    for unit in frozen["units"][len(state["sources"]) :]:
        path = run_root / "compile-units" / unit["input_path"]
        if digest(path) != unit["input_sha256"]:
            raise CompilationError("frozen unit input drift")
        before = _tree(stage)
        before_pages = {
            name: (stage / name).read_text(encoding="utf-8")
            for name in before
            if name.startswith(("wiki/concepts/", "wiki/entities/"))
        }
        first_attempt = max((row["id"] for row in history(run_root)["attempts"]), default=0)
        policy_path.write_text(unit["native_policy"], encoding="utf-8")
        try:
            report = compile_with_recovery(
                stage, [path], directory / unit["id"], command=command, execution_root=run_root
            )
        finally:
            if original is None:
                policy_path.unlink(missing_ok=True)
            else:
                policy_path.write_bytes(original)
        source = {
            **report["sources"][0],
            "unit": unit,
            "execution": [row for row in history(run_root)["attempts"] if row["id"] > first_attempt],
        }
        after = _tree(stage)
        changes = {
            name: {
                "before": sha,
                "after": after.get(name),
                "diff": "".join(
                    difflib.unified_diff(
                        before_pages[name].splitlines(keepends=True),
                        (stage / name).read_text(encoding="utf-8").splitlines(keepends=True) if name in after else [],
                        fromfile=name,
                        tofile=name,
                    )
                ),
            }
            for name, sha in before.items()
            if name.startswith(("wiki/concepts/", "wiki/entities/")) and after.get(name) != sha
        }
        # Retain actual before/after bodies privately for review, not just page counts.
        state["retention"].append(
            {
                "unit": unit["id"],
                "changed_existing_pages": changes,
                "native_receipt": next((directory / unit["id"]).glob("*/completed.json"))
                .relative_to(run_root)
                .as_posix(),
            }
        )
        state["sources"].append(source)
        state["outputs"] = after
        write_json(checkpoint, state)
    bundles = {
        name: json.loads((run_root / "compiler-sources" / key / "bundle.json").read_text())
        for name, key in frozen["bundles"].items()
    }
    publish_unit_assets(run_root, stage / "wiki", bundles, frozen)
    state["outputs"] = _tree(stage)
    write_json(checkpoint, state)
    report = {
        "state": "COMPLETE",
        "policy": POLICY,
        "versions": frozen["toolchain"]["versions"],
        "sources": state["sources"],
        "outputs": _tree(stage),
        "compile_plan": frozen,
        "retention": state["retention"],
    }
    _apply_candidate(stage, kb, state["identity"]["baseline"])
    write_json(directory / "completed.json", {"report": report, "outputs": _tree(kb)})
    return report


def publish_unit_assets(run_root, wiki, bundles, frozen):
    """Verify native unit images, then reuse the original bundle exporter contract."""
    for unit in frozen["units"]:
        for asset in unit["assets"]:
            record = asset["record"]
            if "derivative_path" not in record:
                continue
            name = pathlib.Path(record["derivative_path"]).name
            source = wiki / "sources/images" / unit["id"] / name
            if digest(source) != record["derivative_sha256"]:
                raise CompilationError("native unit asset drift")
            target = wiki / "sources/images" / bundles[asset["source_file"]]["key"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for bundle in bundles.values():
        publish_bundle(run_root / "compiler-sources" / bundle["key"], bundle, wiki, bundle["key"])
