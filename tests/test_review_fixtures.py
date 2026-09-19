"""Characterize existing consumers; this is not the pending compiled-wiki audit."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_fabric.compilation import CompilationError, _git
from research_fabric.run_state import _publication_outputs, _verify_compiled_outputs

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


def test_publication_inventory_includes_modified_and_untracked_fixture_files(candidate):
    before = _git(candidate, "rev-parse", "HEAD")
    outputs = _publication_outputs(candidate)
    assert set(outputs) == {
        path.relative_to(FIXTURE / "after").as_posix() for path in (FIXTURE / "after").rglob("*") if path.is_file()
    }
    assert "M wiki/entities/sensor.md" in _git(candidate, "status", "--porcelain")
    assert "?? wiki/concepts/" in _git(candidate, "status", "--porcelain")
    assert "wiki/concepts/drift.md" in outputs
    assert _git(candidate, "rev-parse", "main") == before


def test_existing_compile_byte_check_rejects_changed_candidate(candidate):
    compiled = {
        "outputs": _publication_outputs(candidate),
        "sources": [
            {
                "summary": "study-a",
                "required": [
                    ["concepts", "create", "drift"],
                    ["entities", "update", "sensor"],
                ],
            }
        ],
    }
    _verify_compiled_outputs(_publication_outputs(candidate), compiled)
    page = candidate / "wiki/concepts/drift.md"
    page.write_text(page.read_text() + "\nChanged after byte snapshot.\n")
    with pytest.raises(CompilationError, match="changed or disappeared.*drift.md"):
        _verify_compiled_outputs(_publication_outputs(candidate), compiled)


@pytest.mark.parametrize("variant", ["qualifier", "count", "relocation", "broken-page", "broken-asset"])
def test_current_evidence_gates_do_not_audit_fixture_wiki_prose_or_links(candidate, variant):
    _git(candidate, "apply", "--unidiff-zero", str(FIXTURE / "variants" / f"{variant}.patch"))
    for gate in ("provenance_validate.py", "excerpt_grounding.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / gate), str(candidate)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    claims = [json.loads(line) for line in (candidate / "evidence/claims.jsonl").read_text().splitlines()]
    assert len(claims) == 2


def test_wiki_only_untracked_addition_is_in_existing_publication_inventory(candidate):
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "Candidate fixture baseline")
    page = candidate / "wiki/concepts/wiki-only.md"
    page.write_text("# Navigation\n\nSee [[sensor]] for the study-specific evidence.\n")
    assert _git(candidate, "diff", "--", "evidence") == ""
    assert "wiki/concepts/wiki-only.md" in _publication_outputs(candidate)
    assert "?? wiki/concepts/wiki-only.md" in _git(candidate, "status", "--porcelain")
