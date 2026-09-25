"""Derived context stays out of source identity and is bound to evidence reuse."""

import copy
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from research_fabric.claims import atomic_write_json
from research_fabric.derived_context import context_path, load_context, prepare_contexts
from research_fabric.engine import ResearchRun, RunConfig
from research_fabric.sources import representation_for, source_attestation

ROOT = Path(__file__).resolve().parents[1]
NOTE = "Possibly E = m c^2; the exponent is uncertain. Ask whether energy scales quadratically with c."


def reading_run(tmp_path, *, shared=False):
    pytest.importorskip("openkb")
    root, run_root = tmp_path / "sources", tmp_path / "run"
    for folder in ("a", "b"):
        (root / folder).mkdir(parents=True)
        image = Image.new("RGB", (240, 60), "white")
        ImageDraw.Draw(image).text((10, 20), "E = m c^2", fill="black")
        image.save(root / folder / "equation.png")
    texts = {
        "a/paper.md": "# Energy\n\nThe relation is illustrated below.\n\n![Equation](equation.png)\n",
        "b/paper.md": "# Method\n\nMeasurements require calibration.\n\n![Calibration](equation.png)\n",
    }
    rows = []
    for name, text in texts.items():
        path = root / name
        path.write_text(text)
        rows.append(
            {
                **source_attestation(representation_for(path), root),
                "source_id": name,
                "url": "https://example.invalid/" + name,
                "title": name,
                "retrieved_at": "2026-01-01",
                "content_type": "text/markdown",
                "snapshot": name,
            }
        )
    profile = {
        "sources": {"a/paper.md": "A", "b/paper.md": "B"},
        "works": {"A": {"title": None, "authors": None}, "B": {"title": None, "authors": None}},
    }
    if shared:
        profile["works"]["B"]["context"] = ["a/paper.md#L1-5"]
    project = {"reading": profile, "corpus_dir": "sources", "manifest_path": "manifest.jsonl"}
    import yaml

    project_path = tmp_path / "project.yaml"
    project_path.write_text(yaml.safe_dump(project))
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(map(json.dumps, rows)) + "\n")
    from research_fabric.visual_preflight import digest

    spec = {
        "model": "chatgpt/gpt-6-astra",
        "effort": "medium",
        "instructions": "Configured inspection instruction.",
        "assets": [
            {
                "path": "a/equation.png",
                "sha256": digest((root / "a/equation.png").read_bytes()),
                "source_locator": "a/paper.md#equation",
            }
        ],
    }
    spec_path = tmp_path / "visual.json"
    atomic_write_json(spec_path, spec)
    run = ResearchRun(
        RunConfig(
            ROOT,
            project_path,
            tmp_path / "kb",
            run_root,
            root,
            "How does energy scale, and what measurement limitations apply?",
            visual_preflight_spec=spec_path,
        ),
        None,
    )
    run.project, run.canonical_manifest, run.acceptance, run.is_multi = project, manifest, {}, False
    run._sources()
    return run, spec, texts


def test_text_only_missing_required_budget_and_shared_context(tmp_path):
    run, spec, _ = reading_run(tmp_path, shared=True)
    plan = run.reading_plan

    def never(*args):
        pytest.fail("text-only run invoked OAuth")

    contexts = prepare_contexts(plan, run.bundles, run.config.run_root, None, never)
    first, second = [r["id"] for r in plan.data["readings"]]
    assert len(contexts[first]["assets"]) == 1
    assert len(contexts[second]["assets"]) == 2
    assert contexts[second]["assets"][0]["source_file"] == "a/paper.md"
    assert all(a["inspection"] is None for a in contexts[second]["assets"])
    with pytest.raises(ValueError, match="required inspection missing"):
        prepare_contexts(plan, run.bundles, tmp_path / "required", None, never, {"required": True})
    with pytest.raises(ValueError, match="budget exceeded"):
        prepare_contexts(plan, run.bundles, tmp_path / "bounded", None, never, {"max_tokens": 1})
    path = context_path(run.config.run_root, first)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        load_context(path)
    # Same basename and bytes are not identity; only the original logical path matches.
    assert (
        run.bundles["a/paper.md"]["assets"][0]["original_sha256"]
        == (run.bundles["b/paper.md"]["assets"][0]["original_sha256"])
    )


def test_context_checksum_and_immutable_history(tmp_path):
    run, _, _ = reading_run(tmp_path)
    contexts = prepare_contexts(run.reading_plan, run.bundles, run.config.run_root, None, None)
    sid = next(iter(contexts))
    path = context_path(run.config.run_root, sid)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="drift"):
        prepare_contexts(run.reading_plan, run.bundles, run.config.run_root, None, None, {"max_tokens": 5000})
    assert path.read_bytes() == before
    damaged = copy.deepcopy(contexts[sid])
    damaged["assets"] = []
    atomic_write_json(path, damaged)
    with pytest.raises(ValueError, match="checksum"):
        load_context(path)
