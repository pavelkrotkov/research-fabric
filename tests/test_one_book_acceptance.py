"""One synthetic book through the real engine; replay only external model I/O."""

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("openkb")

import yaml
from PIL import Image, ImageDraw
from test_native_compilation import engine_run, kb  # noqa: F401
from test_visual_preflight_native import call, message, replay  # noqa: F401
from test_visual_reading_native import _cli_dispatch, _worker_replies

from research_fabric.engine import ResearchRun
from research_fabric.sources import representation_for, source_attestation

FIXTURE = Path(__file__).parent / "fixtures/one_book"


def test_one_book_acceptance(engine_run, replay, tmp_path, monkeypatch):  # noqa: F811
    config, _ = engine_run
    for path in config.source_dir.iterdir():
        path.unlink()
    for chapter in ("a", "b"):
        shutil.copytree(FIXTURE / chapter, config.source_dir / chapter)
    image = Image.new("RGB", (6000, 3000), "white")
    ImageDraw.Draw(image).text((30, 30), "Var(mean) = variance / n", fill="black")
    image.save(config.source_dir / "a/equation.png")
    originals = {
        p.relative_to(config.source_dir).as_posix(): p.read_bytes() for p in config.source_dir.rglob("*") if p.is_file()
    }
    sources = ["a/paper.md", "b/paper.md"]
    rows = [
        {
            **source_attestation(representation_for(config.source_dir / name), config.source_dir),
            "source_id": f"synthetic-{index}",
            "snapshot": name,
            "url": "https://example.invalid/synthetic-book",
            "title": "Synthetic measurement book",
            "retrieved_at": "2026-01-01",
            "content_type": "text/markdown",
        }
        for index, name in enumerate(sources)
    ]
    (config.run_root / "source-manifest.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    project = tmp_path / "one-book.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "corpus_dir": "synthetic",
                "manifest_path": "manifest.jsonl",
                "reading": {
                    "sources": dict.fromkeys(sources, "measurement-book"),
                    "works": {"measurement-book": {"title": None, "authors": None}},
                    "policy": {"target_tokens": 80, "soft_limit_tokens": 120, "context_budget_tokens": 40},
                },
                "acceptance": {"min_claims_per_reading": 1},
                "compilation": {"max_request_tokens": 64000},
            }
        )
    )
    spec = tmp_path / "visual.json"
    spec.write_text(
        json.dumps(
            {
                "model": "chatgpt/gpt-6-astra",
                "effort": "medium",
                "instructions": "Configured inspection instruction.",
                "assets": [
                    {
                        "path": "a/equation.png",
                        "sha256": hashlib.sha256(originals["a/equation.png"]).hexdigest(),
                        "source_locator": "a/paper.md#L15-16",
                    }
                ],
            }
        )
    )
    # Freeze the evaluation before any generation, with source-backed original line ranges.
    freeze = runpy.run_path(str(config.engine_root / "bin/one_book.py"))["freeze_evaluation"]
    evaluation = freeze(config.source_dir, FIXTURE / "evaluation.json", config.run_root)["evaluation"]
    frozen = config.run_root / "acceptance-evaluation.json"
    frozen_bytes = frozen.read_bytes()
    note = "Variance formula is uncertain in this derivative; inspect the original independently."
    outputs, inspections = replay
    outputs.extend([[call()], [message(note)]])
    requests = _worker_replies(monkeypatch)
    native_run = _cli_dispatch(monkeypatch)
    config = replace(config, project_path=project, worker_python=sys.executable, visual_preflight_spec=spec)
    before = subprocess.check_output(["git", "-C", str(config.field_root), "rev-parse", "main"])
    result = ResearchRun(config, lambda **_: "VERDICT: FAIL - offline advisory").execute()
    assert result["state"] == "READY_FOR_REVIEW"
    assert len(inspections) == 2 and len(requests) == 2 and not outputs
    assert note in requests[0]["messages"][0]["content"]
    assert note not in requests[1]["messages"][0]["content"]
    assert frozen.read_bytes() == frozen_bytes
    assert subprocess.check_output(["git", "-C", str(config.field_root), "rev-parse", "main"]) == before
    assert subprocess.check_output(["git", "-C", str(config.field_root), "status", "--porcelain"]) == b""
    coverage = json.loads((config.field_root / "evidence/coverage.json").read_text())
    assert coverage["mechanical_complete"] and coverage["semantic_complete"] is None
    assert coverage["independent_works"] == 1
    assert all(row["disposition"] == "compiled" and row["citing_pages"] for row in coverage["sections"].values())
    assert any(row["changed_existing_pages"] for row in coverage["retention"])
    generated = "\n".join(p.read_text() for p in (config.field_root / "wiki/concepts").glob("*.md"))
    # Canned native replies retain textual qualifiers; equation/figure semantics remain unreviewed.
    for item in evaluation["items"]:
        if item["id"] not in {"equation", "figure"}:
            assert item["expected"] in generated
    packets = json.loads((config.field_root / "evidence/packet-execution.json").read_text())
    visual = next(p["derived_context"] for p in packets.values() if p["derived_context"]["assets"])
    assert not visual["compiler_received"]  # Current native boundary must not be overstated.
    asset = visual["assets"][0]
    assert asset["source_asset"]["renderer"]["operation"] == "downsample"
    assert asset["source_asset"]["original_sha256"] != asset["source_asset"]["derivative_sha256"]
    assert asset["inspection"]["assets"][0]["inspected"]
    assert not asset["inspection"]["assets"][0]["reviewed"]
    for name, content in originals.items():
        assert (config.source_dir / name).read_bytes() == content
    docs = tmp_path / "browser/docs"
    native_run(
        [
            sys.executable,
            str(config.engine_root / "tools/wiki/build_wiki.py"),
            "--vault",
            str(config.field_root / "wiki"),
            "--docs",
            str(docs),
        ],
        check=True,
    )
    assert (docs / "index.md").is_file()
    exported = {hashlib.sha256(p.read_bytes()).hexdigest() for p in (docs / "assets").rglob("*") if p.is_file()}
    assert all(hashlib.sha256(content).hexdigest() in exported for content in originals.values())
    assert asset["source_asset"]["derivative_sha256"] in exported
    shutil.copytree(config.source_dir, config.run_root / "sources")
    # Default platform temp-root (macOS alias included), plus explicit symlink-root regression.
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    alias = tmp_path / "temporary-alias"
    alias.symlink_to(temporary, target_is_directory=True)
    for env in (os.environ.copy(), {**os.environ, "TMPDIR": str(alias)}):
        rehearsal = native_run(
            [
                sys.executable,
                str(config.engine_root / "bin/dryrun_publication.py"),
                str(config.run_root),
                str(config.field_root),
                "--branch",
                "agent/engine",
                "--project",
                "one-book",
                "--projects-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert rehearsal.returncode == 0, rehearsal.stdout + rehearsal.stderr
    assert frozen.read_bytes() == frozen_bytes
    assert subprocess.check_output(["git", "-C", str(config.field_root), "status", "--porcelain"]) == b""
