"""Source-adapter and representation-provenance contract tests."""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

from research_fabric.sources import adapter_for, bind_manifest, discover_sources, representation_for

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "bin" / "provenance_validate.py"


def _html(path: pathlib.Path, body="<p>Hello <b>world</b></p>") -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_invalid_utf8_fails_closed(tmp_path):
    source = tmp_path / "bad.html"
    source.write_bytes(b"<p>ok</p>\xff")
    with pytest.raises(UnicodeDecodeError):
        representation_for(source)


def test_missing_manifest_entry_fails(tmp_path):
    source = _html(tmp_path / "book-1.html")
    with pytest.raises(RuntimeError, match="no entry"):
        bind_manifest([source], [])


def test_discovery_rejects_ambiguous_same_stem(tmp_path):
    _html(tmp_path / "book-1.html")
    _html(tmp_path / "book-1.htm")
    with pytest.raises(RuntimeError, match="ambiguous duplicate"):
        discover_sources(tmp_path)


def test_html_path_locator_and_representation_mapping(tmp_path):
    source = _html(tmp_path / "book-1.html")
    adapter = adapter_for(source)
    rep = representation_for(source)
    assert adapter.metadata(source) == {"content_type": "text/html", "filename": "book-1.html"}
    assert adapter.map_locator("  Book 1 [1]-[4]  ") == "Book 1 [1]-[4]"
    assert rep.text == "Hello world"
    assert rep.original_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert rep.representation_sha256 == hashlib.sha256(b"Hello world").hexdigest()


def _ledger(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    root = tmp_path / "kb"
    snapshots = root / "evidence" / "snapshots"
    snapshots.mkdir(parents=True)
    source = _html(snapshots / "book-1.html")
    rep = representation_for(source)
    source_row = {
        "source_id": "s-1",
        "url": "u",
        "title": "t",
        "retrieved_at": "2026-01-01",
        "content_type": "text/html",
        "sha256": rep.original_sha256,
        "snapshot": "evidence/snapshots/book-1.html",
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
        **overrides,
    }
    claim = {
        "claim_id": "c-1",
        "claim": "claim",
        "note": "wiki/x.md",
        "source_ids": ["s-1"],
        "locator": "Book 1",
        "excerpt": "Hello world",
        "stance": "supports",
        "confidence": 0.9,
        "independence_group": "g",
        "verified_at": "test",
    }
    (root / "evidence" / "sources.jsonl").write_text(json.dumps(source_row) + "\n", encoding="utf-8")
    (root / "evidence" / "claims.jsonl").write_text(json.dumps(claim) + "\n", encoding="utf-8")
    return root


def test_converter_version_drift_fails_closed(tmp_path):
    root = _ledger(tmp_path, adapter_version="old")
    result = subprocess.run([sys.executable, str(PROVENANCE), str(root)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "adapter_version mismatch" in result.stderr


def test_representation_digest_tamper_fails_closed(tmp_path):
    root = _ledger(tmp_path, representation_sha256="0" * 64)
    result = subprocess.run([sys.executable, str(PROVENANCE), str(root)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "representation_sha256 mismatch" in result.stderr
