"""Frozen reading assignments preserve original source boundaries and identities."""

import pytest

from research_fabric.reading import prepare_reading_plan
from research_fabric.sources import representation_for, source_attestation


def prepare(tmp_path, texts, **profile_changes):
    root = tmp_path / "sources"
    root.mkdir(exist_ok=True)
    rows = []
    for name, text in texts.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        rows.append({**source_attestation(representation_for(path), root), "source_id": name, "snapshot": name})
    profile = {
        "works": {"work": {"title": None, "authors": None}},
        "sources": dict.fromkeys(texts, "work"),
        "policy": {"target_tokens": 12, "soft_limit_tokens": 24, "context_budget_tokens": 12},
        **profile_changes,
    }
    project = {"reading": profile}
    plan, bundles = prepare_reading_plan(root, project, rows, tmp_path / "reading-plan.json", tmp_path / "bundles")
    return plan, bundles, root, project, rows


def test_crlf_quote_mapping_headers_nested_identity_and_resume(tmp_path):
    texts = {
        "chapter/source.md": "# Same\r\n\r\nα first argument.\r\n\r\n# Same\r\n\r\nβ second argument.\r\n",
        "paper/source.md": "# Same\r\n\r\nα first argument.\r\n\r\n# Same\r\n\r\nβ second argument.\r\n",
    }
    plan, bundles, root, project, rows = prepare(tmp_path, texts)
    assert len({bundle["key"] for bundle in bundles.values()}) == 2
    assert {r["work_id"] for r in plan.data["readings"]} == {"work"}
    reading = next(r for r in plan.data["readings"] if "α" in plan.input(r["id"]))
    claim = {"source_file": "chapter/source.md", "excerpt": "α first argument."}
    binding, quote = plan.claim(reading["id"], claim)
    assert binding["role"] == "primary"
    assert texts[claim["source_file"]].encode()[slice(*quote["original_bytes"])] == claim["excerpt"].encode()
    with pytest.raises(ValueError, match="outside"):
        plan.claim(reading["id"], {**claim, "excerpt": "PRIMARY SOURCE chapter/source.md"})
    frozen = (tmp_path / "reading-plan.json").read_bytes()
    resumed, _ = prepare_reading_plan(root, project, rows, tmp_path / "reading-plan.json", tmp_path / "bundles")
    assert resumed.data == plan.data and (tmp_path / "reading-plan.json").read_bytes() == frozen
    project["reading"]["policy"]["target_tokens"] += 1
    with pytest.raises(ValueError, match="drift"):
        prepare_reading_plan(root, project, rows, tmp_path / "reading-plan.json", tmp_path / "bundles")


def test_coherent_blocks_definitions_and_context_are_mapped(tmp_path):
    text = (
        "# Argument\n\nUses [notation][r] and note[^n].\n\n$$\n# formula text\nx=1\n$$\n\n"
        "```md\n# fenced heading\n```\n\n| x | y |\n| - | - |\n| 1 | 2 |\n\n"
        "# Definitions\n\n[r]: https://example.invalid\n\n[^n]: A shared definition.\n"
    )
    plan, _, _, _, _ = prepare(tmp_path, {"chapter.md": text})
    assert len(plan.data["sections"]) == 2
    reading = plan.data["readings"][0]
    assert reading["token_counts"]["primary"] > plan.data["policy"]["soft_limit_tokens"]
    assert reading["context"] == [list(plan.data["sections"])[1]]
    body = plan.input(reading["id"])
    assert "$$\n# formula text\nx=1\n$$" in body and "```md\n# fenced heading\n```" in body
    binding, _ = plan.claim(reading["id"], {"source_file": "chapter.md", "excerpt": "A shared definition."})
    assert binding["role"] == "context" and binding["work_id"] == "work"


def test_ambiguous_quote_and_plan_gaps_fail_closed(tmp_path):
    plan, _, _, _, _ = prepare(tmp_path, {"a.md": "# Repetition\n\nSame quote. Same quote.\n"})
    with pytest.raises(ValueError, match="ambiguous"):
        plan.claim(plan.data["readings"][0]["id"], {"source_file": "a.md", "excerpt": "Same quote."})
    from research_fabric.claims import stable_revision

    next(iter(plan.data["sections"].values()))["lines"][0] = 2
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    with pytest.raises(ValueError, match="gaps"):
        plan.validate()


def test_filename_is_not_bibliographic_evidence(tmp_path):
    with pytest.raises(ValueError, match="bibliographic"):
        prepare(
            tmp_path,
            {"2024-famous-author.md": "# An unattributed argument\n"},
            works={"work": {"title": None, "authors": ["Famous Author"]}},
        )


def test_tokenizer_missing_cache_fails_without_download(tmp_path, monkeypatch):
    from research_fabric.sources import _reading_tokenizer

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="provision verified"):
        _reading_tokenizer()


def test_context_keeps_its_source_work_and_reading_role_after_acceptance(tmp_path):
    from research_fabric.claims import ClaimIdentityError, accept_packet, transition_claim
    from research_fabric.sources import reading_quote, source_provenance

    plan, _, root, _, _ = prepare(
        tmp_path,
        {"a.md": "# A\n\nAlpha claim.\n", "b.md": "# B\n\nBeta context. Other context.\n"},
        sources={"a.md": "A", "b.md": "B"},
        works={"A": {"title": None, "authors": None, "context": ["b.md#L1-3"]}, "B": {"title": None, "authors": None}},
    )
    reading = next(row for row in plan.data["readings"] if row["work_id"] == "A")
    claim = {
        "source_file": "b.md",
        "excerpt": "Beta context.",
        "claim": "A contextual statement",
        "locator": "L3",
        "stance": "qualifies",
    }
    binding, before = plan.claim(reading["id"], claim)
    assert binding["work_id"] == "B" and binding["role"] == "context"
    claim["reading"] = binding
    assert plan.project_claim(claim)["independence_group"] is None
    with pytest.raises(ValueError, match="too few primary"):
        plan.validate_packet({"worker": reading["id"], "parsed": {"claims": [claim]}}, reading["id"], {})
    packet = accept_packet(
        {
            "parsed": {"claims": [claim]},
            "source_provenance": source_provenance([root / "a.md", root / "b.md"], source_root=root),
        },
        reading["id"],
        source_dir=root,
    )
    cid = claim["claim_id"]
    transition_claim(
        packet, cid, action="repair", reason="more precise", attempt_id="repair", new_excerpt="Other context."
    )
    accept_packet(packet, reading["id"], source_dir=root)
    after = reading_quote(plan.representations["b.md"], claim)
    assert before != after and claim["claim_id"] == cid and claim["reading"] == binding
    assert (root / "b.md").read_bytes()[slice(*after["original_bytes"])] == b"Other context."
    claim["reading"]["role"] = "primary"
    with pytest.raises(ClaimIdentityError, match="binding changed"):
        accept_packet(packet, reading["id"], source_dir=root)
    assert packet["claim_source_bindings"][cid]["reading"]["role"] == "context"


@pytest.mark.parametrize(
    "block",
    [
        "```md\ninside fence\n```\n",
        "| a | b |\n| - | - |\n| 1 | 2 |\n",
        "$$\nx=1\n$$\n",
        '[ref]:\n  https://example.invalid\n  "Reference title"\n',
    ],
)
def test_reviewed_plan_cannot_split_coherent_blocks(tmp_path, block):
    import json

    reviewed = tmp_path / "reviewed.json"
    sections = {
        "first": {"source_file": "a.md", "lines": [1, 2], "disposition": "primary"},
        "second": {"source_file": "a.md", "lines": [2, 4], "disposition": "primary"},
    }
    reviewed.write_text(
        json.dumps(
            {
                "sections": sections,
                "readings": [{"id": "read-custom", "work_id": "work", "primary": ["first", "second"], "context": []}],
            }
        )
    )
    with pytest.raises(ValueError, match="coherent"):
        prepare(tmp_path, {"a.md": block}, reviewed_plan=str(reviewed))
    assert not (tmp_path / "reading-plan.json").exists()


def test_unknown_disposition_and_context_overlap_fail(tmp_path):
    from research_fabric.claims import stable_revision

    plan, _, _, _, _ = prepare(tmp_path, {"a.md": "# A\n\nAlpha.\n"})
    section = next(iter(plan.data["sections"].values()))
    section.update(disposition="typo", reason="reviewed")
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    with pytest.raises(ValueError, match="unknown.*disposition"):
        plan.validate()
    section["disposition"] = "primary"
    plan.data["readings"][0]["context"] = plan.data["readings"][0]["primary"][:]
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    with pytest.raises(ValueError, match="overlap"):
        plan.validate()


def test_corrupt_tokenizer_cache_never_calls_loader(tmp_path, monkeypatch):
    import hashlib

    import tiktoken

    from research_fabric.sources import _reading_tokenizer

    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    (tmp_path / hashlib.sha1(url.encode()).hexdigest()).write_bytes(b"corrupt encoding")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: pytest.fail("corrupt cache reached tokenizer loader"))
    with pytest.raises(ValueError, match="provision verified"):
        _reading_tokenizer()


def test_nested_bundle_keeps_existing_original_asset_closure(tmp_path):
    from PIL import Image

    from research_fabric._source_assets import verify_bundle
    from research_fabric.sources import copy_source_snapshot

    parent = tmp_path / "sources/nested"
    parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "red").save(parent / "plot.png")
    plan, bundles, root, _, _ = prepare(tmp_path, {"nested/paper.md": "# A\n\nEvidence.\n\n![Plot](plot.png)\n"})
    bundle = bundles["nested/paper.md"]
    assert bundle["assets"][0]["original_path"] == "original/nested/plot.png"
    verify_bundle(tmp_path / "bundles" / bundle["key"], bundle)
    snapshot = copy_source_snapshot(parent / "paper.md", tmp_path / "snapshots", source_root=root)
    assert snapshot.read_bytes() == (parent / "paper.md").read_bytes()
    assert (snapshot.parent / "plot.png").read_bytes() == (parent / "plot.png").read_bytes()
    assert plan.data["readings"][0]["assets"] == [{"source_file": "nested/paper.md", "index": 0}]


@pytest.mark.parametrize("reading_id", ["../escape", "a/b", "", "-option", 7, "a" * 129])
def test_reviewed_reading_id_is_a_safe_worker_identity(tmp_path, reading_id):
    from research_fabric.claims import stable_revision

    plan, _, _, _, _ = prepare(tmp_path, {"a.md": "# A\n\nAlpha.\n"})
    plan.data["readings"][0]["id"] = reading_id
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    with pytest.raises(ValueError, match="reading ID"):
        plan.validate()


def test_section_citation_rejects_unsafe_logical_source_path(tmp_path):
    plan, _, _, _, _ = prepare(tmp_path, {"a.md": "# A\n\nAlpha.\n"})
    section_id = plan.data["readings"][0]["primary"][0]
    source = plan.data["sections"][section_id]["source_file"]
    plan.representations["../a.md"] = plan.representations[source]
    plan.data["sources"]["../a.md"] = plan.data["sources"][source]
    plan.data["sections"][section_id]["source_file"] = "../a.md"
    with pytest.raises(ValueError, match="unsafe reading source path"):
        plan.section(section_id)


def test_frozen_plan_loader_rejects_rehashed_context_and_current_source_drift(tmp_path):
    import json

    from research_fabric.claims import stable_revision
    from research_fabric.reading import ReadingPlan

    plan, _, root, _, _ = prepare(tmp_path, {"a.md": "# A\r\n\r\nAlpha.\r\n", "b.md": "# B\n\nBeta.\n"})
    reading = plan.data["readings"][0]
    claim = {"source_file": "a.md", "excerpt": "Alpha."}
    claim["reading"], _ = plan.claim(reading["id"], claim)
    paths = {name: root / name for name in plan.data["sources"]}
    frozen = tmp_path / "reading-plan.json"
    loaded = ReadingPlan.load(frozen, paths)
    loaded.validate_claim(claim)
    span = loaded.section(reading["primary"][0])
    assert (root / "a.md").read_bytes()[slice(*span["original_bytes"])] == b"# A\r\n\r\nAlpha.\r\n"
    plan.data["readings"][0]["context"] = [plan.data["readings"][1]["primary"][0]]
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    frozen.write_text(json.dumps(plan.data))
    changed = ReadingPlan.load(frozen, paths)
    with pytest.raises(ValueError, match="differs from frozen"):
        changed.validate_claim(claim)
    (root / "b.md").write_text("Changed non-cited context")
    with pytest.raises(ValueError, match="source drift"):
        ReadingPlan.load(frozen, paths)


def test_rehashed_plan_cannot_retarget_actual_repair_before_model_or_write(tmp_path):
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
    from repair_claims import repair_claims

    from research_fabric.claim_report import grounding_report_metadata
    from research_fabric.claims import accept_packet, atomic_write_json, stable_revision
    from research_fabric.sources import source_provenance

    plan, _, root, project, source_rows = prepare(
        tmp_path, {"nested/α paper.md": "# A\n\nFirst line\nsecond line.\n", "b.md": "# B\n\nContext.\n"}
    )
    reading = plan.data["readings"][0]
    claim = {
        "source_file": "nested/α paper.md",
        "excerpt": "First line\nsecond line.",
        "claim": "An argument",
        "stance": "supports",
        "locator": "L3-4",
    }
    claim["reading"], _ = plan.claim(reading["id"], claim)
    claim["excerpt"] = "Rejected quotation"
    packet = accept_packet(
        {
            "parsed": {"claims": [claim]},
            "source_provenance": source_provenance([root / name for name in plan.data["sources"]], source_root=root),
        },
        reading["id"],
        source_dir=root,
    )
    packet_path = tmp_path / "evidence" / f"worker-{reading['id']}.json"
    atomic_write_json(packet_path, packet)
    before = packet_path.read_bytes()
    metadata = grounding_report_metadata(
        [
            {
                **claim,
                "worker": reading["id"],
                "packet_revision": packet["packet_revision"],
                "packet_state_revision": packet["packet_state_revision"],
            }
        ],
        source_rows,
        [(claim["claim_id"], "excerpt")],
    )
    report = tmp_path / "report.txt"
    report.write_text("REPORT-META: " + json.dumps(metadata))
    reading["context"] = plan.data["readings"][1]["primary"][:]
    plan.data["sha256"] = stable_revision({k: v for k, v in plan.data.items() if k != "sha256"})
    atomic_write_json(tmp_path / "reading-plan.json", plan.data)
    with pytest.raises(ValueError, match="differs from frozen"):
        repair_claims(tmp_path, root, report, project=project, model=lambda _: pytest.fail("stale plan reached model"))
    assert packet_path.read_bytes() == before


def test_reuse_cannot_substitute_another_reading_of_the_same_source(tmp_path):
    import json
    from types import SimpleNamespace

    from research_fabric.engine import ResearchRun, _reuse_evidence_packets
    from research_fabric.sources import source_provenance

    plan, _, root, project, _ = prepare(
        tmp_path,
        {"a.md": "# First\n\nFirst unique argument.\n\n# Second\n\nSecond unique argument.\n"},
        policy={"target_tokens": 1},
    )
    first, second = plan.data["readings"]
    claim = {
        "source_file": "a.md",
        "excerpt": "Second unique argument.",
        "claim": "Second",
        "stance": "supports",
        "locator": "second",
    }
    claim["reading"], _ = plan.claim(second["id"], claim)
    packet = {
        "worker": second["id"],
        "parsed": {"claims": [claim]},
        "source_provenance": source_provenance([root / "a.md"], source_root=root),
    }
    reused = tmp_path / "reuse"
    reused.mkdir()
    (reused / f"worker-{first['id']}.json").write_text(json.dumps(packet))
    run = ResearchRun(SimpleNamespace(run_root=tmp_path / "run", field_root=tmp_path / "field"), None)
    run.reading_plan, run.project, run.acceptance, run.is_multi = plan, project, {}, False
    run.worker_provenance = {first["id"]: packet["source_provenance"]}
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="dispatched assignment"):
        run._packet_defects(packet, first["id"])
    with pytest.raises(ValueError, match="dispatched assignment"):
        _reuse_evidence_packets(
            reused,
            destination,
            [(first["id"], "")],
            run.worker_provenance,
            lambda parsed: [],
            root,
            policy=run._packet_policy,
        )
    assert not destination.exists()


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("wrong_source", "source IDs differ"),
        ("extra_source", "source IDs differ"),
        ("missing_binding", "reading"),
        ("malformed_binding", "reading_id"),
        ("missing_plan", "no verified frozen plan"),
        ("malformed_plan", "sources"),
        ("independence", "projection differs"),
    ],
)
def test_actual_gates_reject_invalid_reading_publication(tmp_path, mutation, expected):
    import json
    import subprocess
    import sys
    from pathlib import Path

    plan, _, root, _, rows = prepare(tmp_path, {"a.md": "# A\n\nSame bytes.\n", "b.md": "# A\n\nSame bytes.\n"})
    claim = {
        "source_file": "a.md",
        "excerpt": "Same bytes.",
        "claim": "Evidence",
        "claim_id": "c-test",
        "locator": "L3",
        "stance": "supports",
        "confidence": 0.9,
        "source_ids": ["a.md"],
        "note": "wiki/summaries/a.md",
        "verified_at": "test",
    }
    claim["reading"], _ = plan.claim(plan.data["readings"][0]["id"], claim)
    claim.update(plan.project_claim(claim))
    if mutation == "wrong_source":
        claim["source_ids"] = ["b.md"]
    elif mutation == "extra_source":
        claim["source_ids"] = ["a.md", "b.md"]
    elif mutation == "missing_binding":
        del claim["reading"]
    elif mutation == "malformed_binding":
        claim["reading"] = {}
    elif mutation == "independence":
        claim["independence_group"] = "not-the-cited-work"
    evidence = tmp_path / "field/evidence"
    evidence.mkdir(parents=True)
    if mutation != "missing_plan":
        (evidence / "reading-plan.json").write_text(json.dumps({} if mutation == "malformed_plan" else plan.data))
    snapshots = evidence / "snapshots"
    snapshots.mkdir()
    for row in rows:
        name = row["source_file"]
        (snapshots / name).write_bytes((root / name).read_bytes())
        row.update(
            snapshot="evidence/snapshots/" + name,
            url="https://example.invalid",
            title="Synthetic",
            retrieved_at="2026-01-01",
            content_type="text/markdown",
        )
    (evidence / "sources.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    (evidence / "claims.jsonl").write_text(json.dumps(claim) + "\n")
    for script in ("provenance_validate.py", "excerpt_grounding.py"):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / script), str(evidence.parent)],
            text=True,
            capture_output=True,
        )
        assert result.returncode != 0 and expected in result.stdout + result.stderr
        if script == "excerpt_grounding.py" and mutation != "malformed_plan":
            metadata = json.loads(result.stdout.split("REPORT-META: ", 1)[1].splitlines()[0])
            assert [row["claim_id"] for row in metadata["failures"]] == ["c-test"]
            assert result.stderr.count("c-test:") == 1


@pytest.mark.parametrize("generated", ["wiki/summaries/generated.md", "wiki/concepts/generated.md"])
def test_original_git_bytes_and_whitespace_policy_are_scoped(tmp_path, generated):
    import subprocess

    from research_fabric.run_state import _publication_outputs
    from research_fabric.sources import preserve_original_bytes

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True)

    assert git("init").returncode == 0
    git("config", "core.autocrlf", "true")
    attributes = tmp_path / ".gitattributes"
    attributes.write_bytes(
        b"# Existing policy\r\n*.bin -text\r\nevidence/snapshots/** -text -whitespace\r\n*.md text whitespace\r\n"
    )
    preserve_original_bytes(tmp_path)
    first = attributes.read_bytes()
    preserve_original_bytes(tmp_path)
    assert attributes.read_bytes() == first and first.startswith(b"# Existing policy\n*.bin -text\n")
    original = b"Hard break  \r\n\t\r\nLast line\r\n"
    for relative in (
        "evidence/snapshots/key/a.md",
        "wiki/assets/key/original/nested/a.md",
        "raw/a.md",
        "wiki/sources/a.md",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    assert git("add", ".").returncode == 0
    assert git("diff", "--cached", "--check").returncode == 0
    for relative in (
        "evidence/snapshots/key/a.md",
        "wiki/assets/key/original/nested/a.md",
        "raw/a.md",
        "wiki/sources/a.md",
    ):
        assert git("show", ":" + relative).stdout == original
    assert git("show", ":.gitattributes").stdout == first
    assert ".gitattributes" in _publication_outputs(tmp_path)
    page = tmp_path / generated
    page.parent.mkdir(parents=True)
    page.write_text("Generated trailing whitespace \n")
    git("add", generated)
    result = git("diff", "--cached", "--check")
    assert result.returncode != 0 and generated.encode() in result.stdout


def test_actual_reading_report_repairs_reprojects_and_replays(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
    from repair_claims import repair_claims

    from research_fabric.claim_validation import sync_ledger_rows
    from research_fabric.claims import accept_packet, atomic_write_json
    from research_fabric.sources import source_provenance

    plan, _, root, project, source_rows = prepare(
        tmp_path, {"nested/α paper.md": "# A\r\n\r\nOld quote. Better quote. Surviving quote.\r\n"}
    )
    reading = plan.data["readings"][0]
    claims = []
    for excerpt in ("Old quote.", "Surviving quote."):
        claim = {
            "source_file": "nested/α paper.md",
            "excerpt": excerpt,
            "claim": "Finding",
            "locator": "L3",
            "stance": "supports",
            "confidence": 0.9,
        }
        claim["reading"], _ = plan.claim(reading["id"], claim)
        claims.append(claim)
    old_span = plan.project_claim(claims[0])["quote_span"]
    claims[0]["excerpt"] = "Rejected quotation"
    packet = accept_packet(
        {
            "worker": reading["id"],
            "parsed": {"claims": claims},
            "source_provenance": source_provenance([root / "nested/α paper.md"], source_root=root),
        },
        reading["id"],
        source_dir=root,
    )
    atomic_write_json(tmp_path / "evidence" / f"worker-{reading['id']}.json", packet)
    evidence = tmp_path / "field/evidence"
    evidence.mkdir(parents=True)
    atomic_write_json(evidence / "reading-plan.json", plan.data)
    snapshot = evidence / "snapshots/original.md"
    snapshot.parent.mkdir()
    snapshot.write_bytes((root / "nested/α paper.md").read_bytes())
    source_rows[0].update(
        snapshot="evidence/snapshots/original.md",
        title="Synthetic",
        url="https://example.invalid",
        retrieved_at="2026-01-01",
        content_type="text/markdown",
    )
    (evidence / "sources.jsonl").write_text(json.dumps(source_rows[0]) + "\n")
    rows = [
        {
            **claim,
            "worker": reading["id"],
            "source_ids": [source_rows[0]["source_id"]],
            "note": "wiki/summaries/source.md",
            "verified_at": "fixture",
            "independence_group": "work",
            "quote_span": old_span if i == 0 else plan.project_claim(claim)["quote_span"],
            "packet_revision": packet["packet_revision"],
            "packet_state_revision": packet["packet_state_revision"],
        }
        for i, claim in enumerate(claims)
    ]
    ledger = evidence / "claims.jsonl"
    ledger.write_text("\n".join(map(json.dumps, rows)) + "\n")
    before = ledger.read_bytes()
    gate = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "bin/excerpt_grounding.py"), str(evidence.parent)],
        text=True,
        capture_output=True,
    )
    assert gate.returncode == 1 and "REPORT-META:" in gate.stdout
    report = tmp_path / "report.txt"
    report.write_text(gate.stdout + gate.stderr)
    result = repair_claims(
        tmp_path,
        root,
        report,
        project=project,
        field_root=evidence.parent,
        model=lambda _: '{"found": true, "excerpt": "Better quote."}',
    )
    assert result["repaired"] == 1 and result["fully_validated"]
    accepted = json.loads((tmp_path / "evidence" / f"worker-{reading['id']}.json").read_text())
    projected = sync_ledger_rows(rows, {reading["id"]: accepted}, plan)
    assert projected[0]["quote_span"] != old_span and projected[0]["reading"] == rows[0]["reading"]
    assert projected[1]["claim_id"] == rows[1]["claim_id"]
    assert ledger.read_bytes() == before
    replay = repair_claims(
        tmp_path,
        root,
        report,
        project=project,
        field_root=evidence.parent,
        model=lambda _: pytest.fail("replay called model"),
    )
    assert replay["repaired"] == 0 and replay["fully_validated"]
