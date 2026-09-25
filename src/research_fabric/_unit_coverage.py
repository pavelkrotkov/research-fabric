"""Mechanical unit dispositions and advisory semantic coverage for publication."""

from collections import Counter

from .claims import stable_revision
from .compilation import CompilationError, write_json


def publish_unit_coverage(field_root, compiled, plan):
    frozen = compiled.get("compile_plan")
    if not frozen:
        return {}
    if plan is None or frozen["reading_plan_sha256"] != plan.data["sha256"]:
        raise CompilationError("compile/reading plan identity mismatch")
    if frozen["sha256"] != stable_revision({k: v for k, v in frozen.items() if k != "sha256"}):
        raise CompilationError("compile plan digest drift")
    units = frozen["units"]
    outcomes = compiled["sources"]
    if len(units) != len(outcomes):
        raise CompilationError("incomplete aggregate unit outcomes")
    notes = {}
    covered = Counter()
    for unit, outcome in zip(units, outcomes):
        reading = plan.reading(unit["reading_id"])
        if outcome.get("unit") != unit or outcome["sha256"] != unit["input_sha256"]:
            raise CompilationError("accepted unit identity drift")
        if unit["work_id"] != reading["work_id"] or outcome["summary"] != unit["id"]:
            raise CompilationError("native unit/Work mapping drift")
        expected_sources = {name: plan.data["sources"][name] for name in plan.source_names(reading["id"])}
        if unit["sources"] != expected_sources or unit["derived_context"]:
            raise CompilationError("unit source or derived-context identity drift")
        if [{k: v for k, v in asset.items() if k != "record"} for asset in unit["assets"]] != reading["assets"]:
            raise CompilationError("unit asset closure drift")
        for role in ("primary", "context"):
            expected = {key: plan.section(key) for key in reading[role]}
            if unit[role] != expected:
                raise CompilationError("compile unit source spans drift")
        covered.update(unit["primary"].keys())
        notes[reading["id"]] = f"wiki/summaries/{outcome['summary']}.md"
    primary = Counter(key for key, span in plan.data["sections"].items() if span["disposition"] == "primary")
    if covered != primary or len(notes) != len(plan.data["readings"]):
        raise CompilationError("required source sections lack complete unit dispositions")
    pages = {
        p.relative_to(field_root).as_posix(): p.read_text(encoding="utf-8")
        for folder in ("concepts", "entities", "summaries")
        for p in (field_root / "wiki" / folder).glob("*.md")
    }
    sections = {}
    for key, span in plan.data["sections"].items():
        target = plan.section(key)["target"]
        sections[key] = {
            "disposition": "compiled" if key in covered else span["disposition"],
            "reason": span.get("reason"),
            "citing_pages": [name for name, body in pages.items() if target in body],
        }
    report = {
        "mechanical_complete": True,
        "independent_works": len(plan.data["works"]),
        "native_source_notes": len(units),
        "sections": sections,
        "retention": compiled.get("retention", []),
        "semantic_complete": None,
        "human_review_required": True,
        "limitations": "Compilation and citation presence do not prove concept coverage or retention. "
        "Inspect missing citations, native operation outcomes and before/after pages; "
        "page count cannot pass this audit.",
    }
    write_json(field_root / "evidence/compile-plan.json", frozen)
    write_json(field_root / "evidence/unit-outcomes.json", outcomes)
    write_json(field_root / "evidence/coverage.json", report)
    return notes
