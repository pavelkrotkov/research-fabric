"""Source-adapter and representation-provenance contract tests."""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

from research_fabric.sources import adapter_for, bind_manifest, discover_sources, representation_for, source_bundle

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "bin" / "provenance_validate.py"


def _html(path: pathlib.Path, body="<p>Hello <b>world</b></p>") -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    return path


def _md(path: pathlib.Path, body="# Hello\n\nworld\n") -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    return path


def _manifest_row(source: pathlib.Path) -> dict:
    rep = representation_for(source)
    row = {
        "snapshot": source.name,
        "sha256": rep.original_sha256,
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
    }
    if rep.assets_sha256:
        row["assets_sha256"] = rep.assets_sha256
    return row


@pytest.mark.parametrize("suffix", [".html", ".md"])
def test_invalid_utf8_fails_closed(tmp_path, suffix):
    source = tmp_path / f"bad{suffix}"
    source.write_bytes(b"ok\xff")
    with pytest.raises(UnicodeDecodeError):
        representation_for(source)


def test_missing_manifest_entry_fails(tmp_path):
    source = _html(tmp_path / "book-1.html")
    with pytest.raises(RuntimeError, match="no entry"):
        bind_manifest([source], [])


def test_discovery_rejects_ambiguous_same_stem(tmp_path):
    _html(tmp_path / "book-1.html")
    _md(tmp_path / "book-1.md")
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


def test_markdown_preserves_structure_math_and_assets(tmp_path):
    paths = [
        tmp_path / "chart.png",
        tmp_path / "assets" / "chart(1).png",
        tmp_path / "assets" / "ref.png",
        tmp_path / "assets" / "shortcut.png",
    ]
    (tmp_path / "assets").mkdir()
    for path in paths:
        path.write_bytes(path.name.encode())
    raw = (
        "---\r\ntitle: Sample\r\n---\r\n# Heading\r\n\r\n"
        "Paragraph with **bold**, *emphasis*, [link](https://example.com), and $x^2$.\r\n\r\n"
        "| a | b |\r\n| - | - |\r\n| 1 | 2 |\r\n\r\n$$\r\nE = mc^2\r\n$$\r\n\r\n"
        "````md\r\n```\r\n![not-an-asset](missing.png)\r\n```\r\n````\r\n\r\n"
        "![Same](chart.png)\r\n![Paren](assets/chart(1).png)\r\n![Ref][plot]\r\n![Shortcut]\r\n"
        "[plot]: assets/ref.png\r\n[shortcut]: assets/shortcut.png\r\n"
        "![Remote](https://example.com/chart.png)\r\n"
    )
    source = tmp_path / "book-1.md"
    source.write_bytes(raw.encode())
    adapter = adapter_for(source)
    rep = representation_for(source)
    expected_assets = ("chart.png", "assets/chart(1).png", "assets/ref.png", "assets/shortcut.png")
    assert adapter.metadata(source) == {"content_type": "text/markdown", "filename": "book-1.md"}
    assert adapter.map_locator("  Heading > table row 1  ") == "Heading > table row 1"
    assert rep.text == raw.replace("\r\n", "\n")
    assert rep.assets == expected_assets
    assert source_bundle(source) == ((source, pathlib.Path("book-1.md")), *zip(paths, map(pathlib.Path, expected_assets)))


def test_markdown_missing_or_unsafe_assets_fail_closed(tmp_path):
    missing = _md(tmp_path / "missing.md", "![Chart](assets/missing.png)\n")
    with pytest.raises(FileNotFoundError, match="missing source asset"):
        representation_for(missing)
    unsafe = _md(tmp_path / "unsafe.md", "![Chart](../chart.png)\n")
    with pytest.raises(ValueError, match="unsafe source asset path"):
        representation_for(unsafe)


def test_markdown_asset_change_invalidates_provenance(tmp_path):
    asset = tmp_path / "chart.png"
    asset.write_bytes(b"one")
    source = _md(tmp_path / "book-1.md", "![Chart](chart.png)\n")
    row = _manifest_row(source)
    assert bind_manifest([source], [row]) == [row]
    asset.write_bytes(b"two")
    with pytest.raises(RuntimeError, match="assets_sha256"):
        bind_manifest([source], [row])


@pytest.mark.parametrize("field,value", [("adapter_version", "old"), ("representation_sha256", "0" * 64)])
def test_bind_manifest_rejects_existing_representation_drift(tmp_path, field, value):
    source = _html(tmp_path / "book-1.html")
    row = _manifest_row(source)
    row[field] = value
    with pytest.raises(RuntimeError, match="representation drift"):
        bind_manifest([source], [row])


def test_markdown_formula_change_invalidates_representation(tmp_path):
    source = _md(tmp_path / "book-1.md", "# Equation\n\n$x = 1$\n")
    row = _manifest_row(source)
    source.write_text("# Equation\n\n$x = 2$\n", encoding="utf-8")
    row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="representation drift"):
        bind_manifest([source], [row])


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
