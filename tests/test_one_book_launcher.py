"""Acceptance opt-in and predeclared evaluation checks need no native model runtime."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/one_book.py"


def test_opt_in_and_frozen_source_evaluation(tmp_path):
    denied = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.json")], capture_output=True, text=True
    )
    assert denied.returncode == 2 and "--live is required" in denied.stderr
    assert not list(tmp_path.iterdir())
    freeze = runpy.run_path(str(SCRIPT))["freeze_evaluation"]
    fixture = ROOT / "tests/fixtures/one_book"
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_bytes((fixture / "evaluation.json").read_bytes())
    run = tmp_path / "run"
    original = freeze(fixture, evaluation, run)
    assert freeze(fixture, evaluation, run) == original
    content = json.loads(evaluation.read_text())
    content["sampling"] += " Changed."
    evaluation.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="evaluation drift"):
        freeze(fixture, evaluation, run)
    (tmp_path / "started").mkdir()
    (tmp_path / "started/run.json").write_text("{}")
    with pytest.raises(ValueError, match="after generation"):
        freeze(fixture, evaluation, tmp_path / "started")
    content["items"][0]["expected"] = "Invented quotation"
    evaluation.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="absent from original"):
        freeze(fixture, evaluation, tmp_path / "new")
    assert not (tmp_path / "new").exists()
