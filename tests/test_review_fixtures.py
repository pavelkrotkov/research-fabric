"""Compiled-wiki review covers candidate bytes while semantics stay advisory."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_fabric.compilation import CompilationError, _git
from research_fabric.review import advisory_record, audit_compiled_wiki
from research_fabric.run_state import _publication_outputs, _verify_compiled_outputs, finalize_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/wiki_review"


@pytest.fixture
def candidate(tmp_path):
    kb = tmp_path / "kb"
    entity = kb / "wiki/entities/sensor.md"
    entity.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "before/sensor.md", entity)
    _git(kb, "init", "-b", "main")
    _git(kb, "config", "user.email", "fixture@example.invalid")
    _git(kb, "config", "user.name", "Fixture")
    _git(kb, "add", ".")
    _git(kb, "commit", "-m", "Retained prior entity")
    _git(kb, "switch", "-c", "agent/review-fixture")
    shutil.copytree(FIXTURE / "after", kb, dirs_exist_ok=True)
    return kb


def test_publication_inventory_and_compile_bytes_cover_candidate(candidate):
    before = _git(candidate, "rev-parse", "HEAD")
    outputs = _publication_outputs(candidate)
    expected = {
        path.relative_to(FIXTURE / "after").as_posix() for path in (FIXTURE / "after").rglob("*") if path.is_file()
    }
    assert set(outputs) == expected
    compiled = {
        "outputs": outputs,
        "sources": [
            {
                "summary": "study-a",
                "required": [["concepts", "create", "drift"], ["entities", "update", "sensor"]],
            }
        ],
    }
    _verify_compiled_outputs(outputs, compiled)
    page = candidate / "wiki/concepts/drift.md"
    page.write_text(page.read_text() + "\nChanged after byte snapshot.\n")
    with pytest.raises(CompilationError, match="changed or disappeared.*drift.md"):
        _verify_compiled_outputs(_publication_outputs(candidate), compiled)
    assert _git(candidate, "rev-parse", "main") == before


@pytest.mark.parametrize(
    ("variant", "blocked"),
    [
        ("qualifier", False),
        ("count", False),
        ("relocation", False),
        ("broken-page", True),
        ("broken-asset", True),
    ],
)
def test_compiled_wiki_audit_separates_mechanical_from_semantic_variants(candidate, tmp_path, variant, blocked):
    _git(candidate, "apply", "--unidiff-zero", str(FIXTURE / "variants" / f"{variant}.patch"))
    review = tmp_path / "review"
    if blocked:
        with pytest.raises(CompilationError, match="missing (wiki|local)"):
            audit_compiled_wiki(candidate, review)
        assert not json.loads((review / "compiled-wiki-review.json").read_text())["mechanical"]["ok"]
        return
    report = audit_compiled_wiki(candidate, review)
    assert report["mechanical"]["ok"]
    assert "wiki/entities/sensor.md" in report["changed"]
    assert "wiki/concepts/drift.md" in report["outputs"]
    assert "wiki/concepts/drift.md" in (review / "generated-diff.patch").read_text()


def test_existing_evidence_gates_still_do_not_decide_semantic_fixture(candidate):
    _git(candidate, "apply", "--unidiff-zero", str(FIXTURE / "variants/qualifier.patch"))
    for gate in ("provenance_validate.py", "excerpt_grounding.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / gate), str(candidate)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_wiki_only_untracked_page_is_in_review_scope(candidate, tmp_path):
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "Candidate fixture baseline")
    page = candidate / "wiki/concepts/wiki-only.md"
    page.write_text("# Navigation\n\nSee [[sensor]] for the study-specific evidence.\n")
    report = audit_compiled_wiki(candidate, tmp_path / "review")
    assert report["changed"] == ["wiki/concepts/wiki-only.md"]
    assert "wiki/concepts/wiki-only.md" in (tmp_path / "review/generated-diff.patch").read_text()


def test_invalid_published_reading_plan_is_mechanical_failure(candidate, tmp_path):
    (candidate / "evidence/reading-plan.json").write_text("{}")
    with pytest.raises(CompilationError, match="invalid published reading plan"):
        audit_compiled_wiki(candidate, tmp_path / "review")


def test_review_snapshot_invalidates_later_candidate_change(candidate, tmp_path):
    report = audit_compiled_wiki(candidate, tmp_path / "review")
    page = candidate / "wiki/entities/sensor.md"
    page.write_text(page.read_text() + "\nLate mutation.\n")
    with pytest.raises(CompilationError, match="changed after compiled-wiki review"):
        finalize_run(candidate, tmp_path / "run", "proposal", 2, lambda: {"fixture": True}, reviewed=report)


def test_advisory_records_distinguish_findings_and_never_become_mechanical_gates():
    replies = json.loads((FIXTURE / "advisory.json").read_text())
    scope = {"candidate_sha256": "fixture", "changed": ["wiki/entities/sensor.md"]}
    records = {name: advisory_record(reply, scope) for name, reply in replies.items()}
    assert {records[name]["status"] for name in ("qualifier", "count", "relocation")} == {"available"}
    assert records["malformed"]["status"] == "malformed"
    assert records["unavailable"]["status"] == "unavailable"
    assert len({records[name]["response"] for name in ("qualifier", "count", "relocation")}) == 3
