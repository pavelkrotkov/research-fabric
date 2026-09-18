"""Regression tests for stable claim identity and ID-addressed repair."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from research_fabric.claims import (
    ClaimIdentityError,
    accept_packet,
    atomic_write_json,
    migrate_legacy_packet,
    packet_state_revision,
    source_revision,
    transition_claim,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
_SPEC = importlib.util.spec_from_file_location("repair_claims", ROOT / "bin" / "repair_claims.py")
repair_claims = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(repair_claims)
_DRY_SPEC = importlib.util.spec_from_file_location("dryrun_publication", ROOT / "bin" / "dryrun_publication.py")
dryrun_publication = importlib.util.module_from_spec(_DRY_SPEC)
assert _DRY_SPEC.loader is not None
_DRY_SPEC.loader.exec_module(dryrun_publication)


def _packet():
    return {
        "worker": "book-1",
        "attempts": [{"attempt": 1}],
        "parsed": {
            "claims": [
                {
                    "claim": "same claim",
                    "source_file": "source.html",
                    "locator": "1",
                    "excerpt": "bad A",
                    "stance": "supports",
                },
                {
                    "claim": "same claim",
                    "source_file": "source.html",
                    "locator": "2",
                    "excerpt": "bad B",
                    "stance": "supports",
                },
                {
                    "claim": "same claim",
                    "source_file": "source.html",
                    "locator": "3",
                    "excerpt": "good C",
                    "stance": "supports",
                },
            ]
        },
    }


def _source(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir(exist_ok=True)
    (source_dir / "source.html").write_text("good A good B good C", encoding="utf-8")
    return source_dir


def test_ids_survive_drop_repair_and_reorder_with_duplicate_values(tmp_path):
    packet = accept_packet(_packet(), "book-1", source_dir=_source(tmp_path), attempt_id="attempt-1")
    ids = [claim["claim_id"] for claim in packet["parsed"]["claims"]]
    assert ids == ["c-book-1-1", "c-book-1-2", "c-book-1-3"]

    packet, dropped = transition_claim(
        packet, ids[0], action="drop", reason="ungrounded", attempt_id="repair-1", report_id="report-1"
    )
    packet, repaired = transition_claim(
        packet,
        ids[1],
        action="repair",
        reason="grounded replacement",
        attempt_id="repair-1",
        report_id="report-1",
        new_excerpt="good B",
    )
    assert dropped["claim_id"] == ids[0]
    assert repaired["claim_id"] == ids[1]
    assert [claim["claim_id"] for claim in packet["parsed"]["claims"]] == ids[1:]

    packet["parsed"]["claims"].reverse()
    packet["packet_state_revision"] = packet_state_revision(packet["parsed"])
    accepted_again = accept_packet(packet, "book-1", source_dir=_source(tmp_path))
    assert [claim["claim_id"] for claim in accepted_again["parsed"]["claims"]] == [ids[2], ids[1]]
    assert accepted_again["parsed"]["claims"][0]["excerpt"] == "good C"


def test_acceptance_rejects_changed_source_representation(tmp_path):
    source_dir = _source(tmp_path)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    packet["parsed"]["claims"][0]["source_file"] = "other.html"
    (source_dir / "other.html").write_text("good A good B good C", encoding="utf-8")
    with pytest.raises(ClaimIdentityError, match="source binding"):
        accept_packet(packet, "book-1", source_dir=source_dir)


def _bound_report(packet, source_dir, failures):
    rows = []
    for failure in failures:
        claim = next(c for c in packet["parsed"]["claims"] if c["claim_id"] == failure["claim_id"])
        rows.append(
            {
                "claim_id": claim["claim_id"],
                "worker": "book-1",
                "packet_revision": packet["packet_revision"],
                "packet_state_revision": packet["packet_state_revision"],
                "source_ids": ["s-1"],
                "source_revision": claim["source_revision"],
                **failure,
            }
        )
    sources = [{"source_id": "s-1", "sha256": hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest()}]
    metadata = {
        "version": 1,
        "source_revision": source_revision(sources),
        "packets": {
            "book-1": {
                "packet_revision": packet["packet_revision"],
                "packet_state_revision": packet["packet_state_revision"],
            }
        },
        "failures": rows,
    }
    metadata["report_id"] = repair_claims.stable_revision(metadata)
    return "REPORT-META: " + json.dumps(metadata, sort_keys=True) + "\n"


def test_repair_uses_ids_and_replay_is_idempotent(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet_input = _packet()
    packet_input["source_provenance"] = [
        {
            "source_file": "source.html",
            "sha256": hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest(),
            "adapter": "html",
            "adapter_version": "1",
            "representation_encoding": "utf-8",
            "representation_sha256": "representation-digest",
        }
    ]
    attestation = packet_input["source_provenance"]
    packet = accept_packet(packet_input, "book-1", source_dir=source_dir, attempt_id="attempt-1")
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)
    ids = [c["claim_id"] for c in packet["parsed"]["claims"]]
    report = tmp_path / "report.txt"
    report.write_text(
        _bound_report(
            packet,
            source_dir,
            [
                {"claim_id": ids[1], "old_excerpt": "bad B", "reason": "excerpt not found"},
                {"claim_id": ids[0], "old_excerpt": "bad A", "reason": "excerpt not found"},
            ],
        ),
        encoding="utf-8",
    )
    calls = []

    def model(prompt):
        calls.append(prompt)
        return '{"excerpt":"good B","found":true}' if ids[1] in prompt else '{"found":false}'

    result = repair_claims.repair_claims(run_root, source_dir, report, model=model)
    assert result["repaired"] == 1 and result["dropped"] == 1
    assert result["fully_validated"] is False
    after = json.loads(packet_path.read_text(encoding="utf-8"))
    remaining = {c["claim_id"]: c for c in after["parsed"]["claims"]}
    assert set(remaining) == {ids[1], ids[2]}
    assert remaining[ids[1]]["excerpt"] == "good B"
    assert remaining[ids[2]]["excerpt"] == "good C"
    history_len = len(after["claim_history"])

    replay = repair_claims.repair_claims(
        run_root, source_dir, report, model=lambda _prompt: pytest.fail("replay called the model")
    )
    assert replay["repaired"] == 0 and replay["dropped"] == 0
    replayed_packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert len(replayed_packet["claim_history"]) == history_len
    assert replayed_packet["source_provenance"] == attestation
    assert len(calls) == 2


def test_unknown_or_stale_report_fails_before_packet_mutation(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    path = packet_dir / "worker-book-1.json"
    atomic_write_json(path, packet)
    before = path.read_bytes()
    unknown = tmp_path / "unknown.txt"
    unknown_metadata = {
        "version": 1,
        "source_revision": source_revision(
            [{"sha256": hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest()}]
        ),
        "packets": {
            "book-1": {
                "packet_revision": packet["packet_revision"],
                "packet_state_revision": packet["packet_state_revision"],
            }
        },
        "failures": [{"claim_id": "c-book-1-99", "worker": "book-1", "old_excerpt": "missing", "reason": "stale"}],
    }
    unknown_metadata["report_id"] = repair_claims.stable_revision(unknown_metadata)
    unknown.write_text("REPORT-META: " + json.dumps(unknown_metadata) + "\n")
    with pytest.raises(ClaimIdentityError, match="unknown"):
        repair_claims.repair_claims(run_root, source_dir, unknown, model=lambda _: "{}")
    assert path.read_bytes() == before

    stale = tmp_path / "stale.txt"
    stale.write_text(
        _bound_report(
            packet,
            source_dir,
            [{"claim_id": "c-book-1-1", "old_excerpt": "bad A", "reason": "stale"}],
        ).replace(
            source_revision([{"sha256": hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest()}]),
            "stale",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ClaimIdentityError, match="source revision"):
        repair_claims.repair_claims(run_root, source_dir, stale, model=lambda _: "{}")
    assert path.read_bytes() == before

    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["parsed"]["claims"][0]["claim"] = "mutated meaning"
    atomic_write_json(path, changed)
    report_for_original = tmp_path / "original.txt"
    report_for_original.write_text(
        _bound_report(
            packet,
            source_dir,
            [{"claim_id": "c-book-1-1", "old_excerpt": "bad A", "reason": "stale"}],
        ),
        encoding="utf-8",
    )
    with pytest.raises(ClaimIdentityError, match="packet state"):
        repair_claims.repair_claims(run_root, source_dir, report_for_original, model=lambda _: "{}")


def test_legacy_positional_report_persists_explicit_mapping(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet_path = packet_dir / "worker-book-1.json"
    original = _packet()
    ledger = [
        dict(
            claim,
            claim_id=f"c-book-1-{index}",
            source_revision=hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest(),
        )
        for index, claim in enumerate(original["parsed"]["claims"], 1)
    ]
    accepted = migrate_legacy_packet(original, "book-1", ledger, source_dir=source_dir)
    atomic_write_json(packet_path, accepted)
    report = tmp_path / "legacy.txt"
    report.write_text(
        "c-book-1-1: excerpt not found in snapshot\nc-book-1-2: excerpt not found in snapshot\n",
        encoding="utf-8",
    )
    repair_claims.repair_claims(
        run_root,
        source_dir,
        report,
        model=lambda prompt: '{"excerpt":"good B","found":true}' if "c-book-1-2" in prompt else '{"found":false}',
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["legacy_claim_id_map"]["c-book-1-2"] == "c-book-1-2"
    claims = {claim["claim_id"]: claim for claim in packet["parsed"]["claims"]}
    assert claims["c-book-1-2"]["excerpt"] == "good B"
    assert "c-book-1-1" not in claims


def test_legacy_shortened_packet_is_rejected_before_guessing_target(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = _packet()
    packet["parsed"]["claims"] = packet["parsed"]["claims"][1:]
    packet["attempts"] = [{"attempt": 1}]
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)
    report = tmp_path / "legacy.txt"
    report.write_text("c-book-1-2: excerpt not found in snapshot\n", encoding="utf-8")
    before = packet_path.read_bytes()
    with pytest.raises(ClaimIdentityError, match="persisted claim identity"):
        repair_claims.repair_claims(run_root, source_dir, report, model=lambda _: '{"found":false}')
    assert packet_path.read_bytes() == before


def test_model_supplied_claim_id_is_not_accepted_as_host_identity(tmp_path):
    packet = _packet()
    packet["parsed"]["claims"][0]["claim_id"] = "model-selected"
    with pytest.raises(ClaimIdentityError, match="before host identity"):
        accept_packet(packet, "book-1", source_dir=_source(tmp_path))


def test_repair_rejects_non_html_source_representation(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "source.md").write_text("a source passage", encoding="utf-8")
    claim = {"claim_id": "c-book-1-1", "source_file": "source.md"}
    with pytest.raises(ClaimIdentityError, match="unsupported source representation"):
        repair_claims._source_body(source_dir, claim)


def test_dryrun_persists_migrated_packet_only_in_disposable_destination(tmp_path):
    source_dir = _source(tmp_path)
    input_path = tmp_path / "worker-book-1.json"
    atomic_write_json(input_path, _packet())
    destination = tmp_path / "disposable" / "accepted-packets"
    accepted, accepted_path = dryrun_publication.accept_and_persist_packet(
        input_path, "book-1", source_dir, destination
    )
    assert not json.loads(input_path.read_text(encoding="utf-8"))["parsed"]["claims"][0].get("claim_id")
    persisted = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert persisted["parsed"]["claims"][0]["claim_id"] == accepted["parsed"]["claims"][0]["claim_id"]
    assert persisted.get("source_provenance") == accepted.get("source_provenance")


def test_drop_last_claim_fails_after_packet_history_is_retained(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = _packet()
    packet["parsed"]["claims"] = packet["parsed"]["claims"][:1]
    packet = accept_packet(packet, "book-1", source_dir=source_dir)
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)
    report = tmp_path / "report.txt"
    report.write_text(
        _bound_report(
            packet,
            source_dir,
            [{"claim_id": "c-book-1-1", "old_excerpt": "bad A", "reason": "excerpt not found"}],
        ),
        encoding="utf-8",
    )
    with pytest.raises(ClaimIdentityError, match="zero claims"):
        repair_claims.repair_claims(run_root, source_dir, report, model=lambda _: '{"found":false}')
    after = json.loads(packet_path.read_text(encoding="utf-8"))
    assert after["parsed"]["claims"] == []
    assert any(event.get("event") == "drop" for event in after["claim_history"])
    sidecar = json.loads((packet_dir / "claim-history.json").read_text(encoding="utf-8"))
    assert any(event.get("event") == "drop" for event in sidecar)


def test_model_error_is_reported_after_prior_history_is_written(tmp_path):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)
    ids = [claim["claim_id"] for claim in packet["parsed"]["claims"]]
    report = tmp_path / "report.txt"
    report.write_text(
        _bound_report(
            packet,
            source_dir,
            [
                {"claim_id": ids[0], "old_excerpt": "bad A", "reason": "excerpt not found"},
                {"claim_id": ids[1], "old_excerpt": "bad B", "reason": "excerpt not found"},
            ],
        ),
        encoding="utf-8",
    )

    def model(prompt):
        if ids[0] in prompt:
            return '{"found":false}'
        raise RuntimeError("provider unavailable")

    with pytest.raises(ClaimIdentityError, match="did not resolve"):
        repair_claims.repair_claims(run_root, source_dir, report, model=model)
    after = json.loads(packet_path.read_text(encoding="utf-8"))
    assert any(event.get("claim_id") == ids[0] and event.get("event") == "drop" for event in after["claim_history"])


def test_sidecar_history_recovers_after_packet_write_before_sidecar(tmp_path, monkeypatch):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)
    ids = [claim["claim_id"] for claim in packet["parsed"]["claims"]]
    report = tmp_path / "report.txt"
    report.write_text(
        _bound_report(
            packet,
            source_dir,
            [
                {"claim_id": ids[0], "old_excerpt": "bad A", "reason": "excerpt not found"},
                {"claim_id": ids[1], "old_excerpt": "bad B", "reason": "excerpt not found"},
            ],
        ),
        encoding="utf-8",
    )
    original_append = repair_claims.append_history
    state = {"entries": 0}

    def fail_once(path, entries):
        if entries:
            state["entries"] += 1
        if state["entries"] == 2:
            raise OSError("interrupted sidecar write")
        return original_append(path, entries)

    monkeypatch.setattr(repair_claims, "append_history", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        repair_claims.repair_claims(
            run_root,
            source_dir,
            report,
            model=lambda prompt: '{"excerpt":"good B","found":true}' if ids[1] in prompt else '{"found":false}',
        )
    monkeypatch.setattr(repair_claims, "append_history", original_append)
    replay = repair_claims.repair_claims(
        run_root,
        source_dir,
        report,
        model=lambda _: pytest.fail("replay called model after packet event persisted"),
    )
    assert replay["repaired"] == 0 and replay["dropped"] == 0
    sidecar = json.loads((packet_dir / "claim-history.json").read_text(encoding="utf-8"))
    assert any(event.get("claim_id") == ids[1] and event.get("event") == "repair" for event in sidecar)


def _workflow_ledger(packet_dir, field_root):
    """Execute the production ledger loop without importing CAO orchestration."""
    tree = ast.parse((ROOT / "workflows" / "research.py").read_text())
    loop = next(
        node
        for node in tree.body
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "results"
        and any(isinstance(child, ast.Name) and child.id == "ledger_claim" for child in ast.walk(node))
    )
    note = field_root / "note.md"
    note.write_text("Native compiler output is outside this identity test.")
    scope = {
        "json": json,
        "pathlib": pathlib,
        "results": [("book-1", "", None)],
        "packet_dir": packet_dir,
        "field_root": field_root,
        "SOURCE_BY_FILE": {"source.html": "s-1"},
        "NOTE_BY_SOURCE": {"s-1": "note.md"},
        "claims": [],
        "dropped": [],
        "IS_MULTI": True,
    }
    exec(compile(ast.Module(body=[loop], type_ignores=[]), str(ROOT / "workflows" / "research.py"), "exec"), scope)
    assert not scope["dropped"]
    return scope["claims"]


@pytest.mark.parametrize("fault", [None, "snapshot", "bounds", "witness_schema", "witness_grounding"])
def test_actual_grounding_report_binds_repair_and_ledger_ids(tmp_path, fault):
    source_dir = _source(tmp_path)
    run_root = tmp_path / "run"
    packet_dir = run_root / "evidence"
    packet_dir.mkdir(parents=True)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    packet_path = packet_dir / "worker-book-1.json"
    atomic_write_json(packet_path, packet)

    field_root = tmp_path / "kb"
    snapshots = field_root / "evidence" / "snapshots"
    snapshots.mkdir(parents=True)
    snapshot = snapshots / "source.html"
    snapshot.write_bytes((source_dir / "source.html").read_bytes())
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (field_root / "evidence" / "sources.jsonl").write_text(
        json.dumps(
            {
                "source_id": "s-1",
                "url": "u",
                "title": "t",
                "retrieved_at": "2026-01-01",
                "content_type": "text/html",
                "sha256": digest,
                "snapshot": "evidence/snapshots/source.html",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claims = _workflow_ledger(packet_dir, field_root)
    (field_root / "evidence" / "claims.jsonl").write_text(
        "\n".join(json.dumps(claim) for claim in claims) + "\n", encoding="utf-8"
    )
    ledger_before_revalidation = (field_root / "evidence" / "claims.jsonl").read_bytes()
    gate = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "excerpt_grounding.py"), str(field_root)],
        capture_output=True,
        text=True,
    )
    assert gate.returncode != 0
    assert "REPORT-META:" in gate.stdout
    report = tmp_path / "actual-report.txt"
    report.write_text(gate.stdout + gate.stderr, encoding="utf-8")
    project = {"acceptance": {"min_claims_per_book": 1, "max_claims_per_book": 30}}
    if fault == "snapshot":
        snapshot.write_text("tampered snapshot", encoding="utf-8")
    if fault == "bounds":
        project["acceptance"]["min_claims_per_book"] = 3
    if fault and fault.startswith("witness"):
        project["witnesses"] = {"test": {"translator": "test"}}
        project["acceptance"]["latin_canonical_required"] = True
        if fault == "witness_grounding":
            for claim in packet["parsed"]["claims"]:
                claim.update(
                    claim_type="canonical",
                    english_witness={
                        "translator": "test",
                        "source_id": "w-test",
                        "locator": "1",
                        "excerpt": "fabricated",
                        "rationale": "test",
                    },
                )
            accept_packet(packet, "book-1", source_dir=source_dir)
            atomic_write_json(packet_path, packet)
            for row, claim in zip(claims, packet["parsed"]["claims"]):
                row.update(
                    packet_state_revision=packet["packet_state_revision"],
                    english_witness=claim["english_witness"],
                    claim_type="canonical",
                )
            (field_root / "evidence" / "claims.jsonl").write_text("\n".join(json.dumps(row) for row in claims) + "\n")
            ledger_before_revalidation = (field_root / "evidence" / "claims.jsonl").read_bytes()
            gate = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "excerpt_grounding.py"), str(field_root)],
                capture_output=True,
                text=True,
            )
            report.write_text(gate.stdout + gate.stderr)
            (run_root / "alignment.jsonl").write_text(
                json.dumps({"book": 1, "witnesses": {"test": {"aligned_to": "Aen.1.1-3"}}}) + "\n"
            )

    def repair():
        return repair_claims.repair_claims(
            run_root,
            source_dir,
            report,
            model=lambda prompt: '{"found":false}' if "c-book-1-1" in prompt else '{"excerpt":"good B","found":true}',
            field_root=field_root,
            project=project,
        )

    if fault:
        errors = {
            "snapshot": "provenance_validate",
            "bounds": "below min_claims",
            "witness_schema": "english_witness",
            "witness_grounding": "translation_grounding",
        }
        with pytest.raises(ClaimIdentityError, match=errors[fault]):
            repair()
        assert (field_root / "evidence" / "claims.jsonl").read_bytes() == ledger_before_revalidation
        return
    result = repair()
    assert result["fully_validated"] is True
    assert result["repaired"] == 1 and result["dropped"] == 1
    after = json.loads(packet_path.read_text(encoding="utf-8"))
    assert [claim["claim_id"] for claim in after["parsed"]["claims"]] == ["c-book-1-2", "c-book-1-3"]
    assert (field_root / "evidence" / "claims.jsonl").read_bytes() == ledger_before_revalidation
    regenerated = _workflow_ledger(packet_dir, field_root)
    assert [row["claim_id"] for row in regenerated] == ["c-book-1-2", "c-book-1-3"]
    assert regenerated[0]["excerpt"] == "good B"
    assert regenerated[1]["excerpt"] == "good C"
    (field_root / "evidence" / "claims.jsonl").write_text(
        "\n".join(json.dumps(row) for row in regenerated) + "\n", encoding="utf-8"
    )
    provenance = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "provenance_validate.py"), str(field_root)],
        capture_output=True,
        text=True,
    )
    assert provenance.returncode == 0, provenance.stderr
    reground = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "excerpt_grounding.py"), str(field_root)],
        capture_output=True,
        text=True,
    )
    assert reground.returncode == 0, reground.stderr


def test_atomic_write_preserves_previous_json_on_replace_error(tmp_path, monkeypatch):
    path = tmp_path / "packet.json"
    atomic_write_json(path, {"valid": 1})
    monkeypatch.setattr("research_fabric.claims.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        atomic_write_json(path, {"valid": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"valid": 1}


@pytest.mark.parametrize("change", ["drop", "reorder"])
def test_legacy_migration_rejects_changed_original_order(tmp_path, change):
    source_dir = _source(tmp_path)
    original = _packet()
    ledger = [
        dict(
            claim,
            claim_id=f"c-book-1-{index}",
            source_revision=hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest(),
        )
        for index, claim in enumerate(original["parsed"]["claims"], 1)
    ]
    if change == "drop":
        original["parsed"]["claims"].pop(0)
    else:
        original["parsed"]["claims"].reverse()
    with pytest.raises(ClaimIdentityError, match="original ledger"):
        migrate_legacy_packet(original, "book-1", ledger, source_dir=source_dir)


def test_host_reacceptance_does_not_authorize_old_positional_report(tmp_path):
    source_dir = _source(tmp_path)
    packet = _packet()
    packet["parsed"]["claims"].pop(0)
    packet = accept_packet(packet, "book-1", source_dir=source_dir)
    path = tmp_path / "run" / "evidence" / "worker-book-1.json"
    atomic_write_json(path, packet)
    report = tmp_path / "old-report.txt"
    report.write_text("c-book-1-2: excerpt not found in snapshot\n")
    before = path.read_bytes()
    with pytest.raises(ClaimIdentityError, match="original published ledger"):
        repair_claims.repair_claims(tmp_path / "run", source_dir, report, model=lambda _: pytest.fail("called model"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("order", [[1, 0], [0, 1]])
def test_multiple_drops_with_identical_records_preserve_third_id(tmp_path, order):
    source_dir = _source(tmp_path)
    packet = _packet()
    packet["parsed"]["claims"][1] = dict(packet["parsed"]["claims"][0])
    packet = accept_packet(packet, "book-1", source_dir=source_dir)
    ids = [claim["claim_id"] for claim in packet["parsed"]["claims"]]
    run = tmp_path / "run"
    path = run / "evidence" / "worker-book-1.json"
    atomic_write_json(path, packet)
    report = tmp_path / "report"
    report.write_text(
        _bound_report(
            packet,
            source_dir,
            [{"claim_id": ids[index], "old_excerpt": "bad A", "reason": "excerpt not found"} for index in order],
        )
    )
    repair_claims.repair_claims(run, source_dir, report, model=lambda _: '{"found":false}')
    after = json.loads(path.read_text())
    assert [claim["claim_id"] for claim in after["parsed"]["claims"]] == [ids[2]]
    assert after["parsed"]["claims"][0]["excerpt"] == "good C"


def test_duplicate_persisted_claim_ids_fail_before_repair(tmp_path):
    source_dir = _source(tmp_path)
    packet = accept_packet(_packet(), "book-1", source_dir=source_dir)
    packet["parsed"]["claims"][1]["claim_id"] = packet["parsed"]["claims"][0]["claim_id"]
    with pytest.raises(ClaimIdentityError, match="duplicate claim_id"):
        accept_packet(packet, "book-1", source_dir=source_dir)


def test_legacy_migration_rejects_changed_source_with_identical_quote(tmp_path):
    source_dir = _source(tmp_path)
    packet = _packet()
    ledger = [
        dict(
            claim,
            claim_id=f"c-book-1-{index}",
            source_revision=hashlib.sha256((source_dir / "source.html").read_bytes()).hexdigest(),
        )
        for index, claim in enumerate(packet["parsed"]["claims"], 1)
    ]
    (source_dir / "other.html").write_bytes((source_dir / "source.html").read_bytes())
    packet["parsed"]["claims"][0]["source_file"] = "other.html"
    with pytest.raises(ClaimIdentityError, match="original ledger"):
        migrate_legacy_packet(packet, "book-1", ledger, source_dir=source_dir)
