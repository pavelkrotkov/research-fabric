"""Source-adapter and representation-provenance contract tests."""

from __future__ import annotations

import ast
import shutil
import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

from research_fabric.sources import (adapter_for, bind_manifest, discover_sources, representation_for, source_bundle, packet_source_defects, source_provenance, source_provenance_errors)

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
        tmp_path / "assets" / "nested.png",
    ]
    (tmp_path / "assets").mkdir()
    for path in paths:
        path.write_bytes(path.name.encode())
    raw = (
        "---\r\ntitle: Sample\r\n---\r\n# Heading\r\n\r\n"
        "Paragraph with **bold**, *emphasis*, [link](https://example.com), and $x^2$.\r\n\r\n"
        "| a | b |\r\n| - | - |\r\n| 1 | 2 |\r\n\r\n$$\r\nE = mc^2\r\n$$\r\n\r\n"
        "````md\r\n```\r\n![not-an-asset](missing.png)\r\n```\r\n````\r\n\r\n"
        "`![inline-example](missing-inline.png)`\r\n\r\n"
        "    ![indented-example](missing-indented.png)\r\n\r\n"
        "<!-- ![commented](missing-comment.png) -->\r\n\r\n"
        "- item\r\n\r\n    ![Nested](assets/nested.png)\r\n\r\n"
        "![A \\] chart](chart.png)\r\n![Paren](assets/chart(1).png)\r\n![Ref][plot]\r\n![Shortcut]\r\n"
        "[plot]: assets/ref.png\r\n[shortcut]: assets/shortcut.png\r\n"
        "![Remote](https://example.com/chart.png)\r\n"
    )
    source = tmp_path / "book-1.md"
    source.write_bytes(raw.encode())
    adapter = adapter_for(source)
    rep = representation_for(source)
    expected_assets = (
        "assets/nested.png",
        "chart.png",
        "assets/chart(1).png",
        "assets/ref.png",
        "assets/shortcut.png",
    )
    expected_paths = [paths[4], *paths[:4]]
    expected_bundle = ((source, pathlib.Path("book-1.md")), *zip(expected_paths, map(pathlib.Path, expected_assets)))
    assert adapter.metadata(source) == {"content_type": "text/markdown", "filename": "book-1.md"}
    assert adapter.map_locator("  Heading > table row 1  ") == "Heading > table row 1"
    assert rep.text == raw.replace("\r\n", "\n")
    assert rep.assets == expected_assets
    assert source_bundle(source) == expected_bundle


def test_markdown_missing_or_unsafe_assets_fail_closed(tmp_path):
    missing = _md(tmp_path / "missing.md", "![Chart](assets/missing.png)\n")
    with pytest.raises(FileNotFoundError, match="missing source asset"):
        representation_for(missing)
    missing_row = {"snapshot": missing.name, "sha256": hashlib.sha256(missing.read_bytes()).hexdigest()}
    with pytest.raises(RuntimeError, match="source representation invalid"):
        bind_manifest([missing], [missing_row])
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


@pytest.mark.parametrize(
    "field,value", [("adapter_version", "old"), ("representation_sha256", "0" * 64)]
)
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


class _TextAdapter:
    name = "text"
    version = "7"
    suffixes = (".txt",)

    def decode(self, raw: bytes) -> str:
        return raw.decode("utf-8")

    def extract_text(self, decoded: str) -> str:
        return decoded.strip()

    def map_locator(self, locator: str) -> str:
        return locator.strip()

    def assets(self, decoded: str) -> tuple[str, ...]:
        return ()

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        return {"content_type": "text/plain", "filename": path.name}


def test_custom_adapter_flows_from_discovery_to_manifest(tmp_path):
    source = tmp_path / "book-1.txt"
    source.write_text("Hello custom", encoding="utf-8")
    adapters = (_TextAdapter(),)
    files = discover_sources(tmp_path, adapters)
    rep = representation_for(source, adapters)
    assert source_provenance(files, adapters)[0]["adapter"] == "text"
    bound = bind_manifest(files, [{"snapshot": source.name, "sha256": rep.original_sha256}], adapters)
    assert bound[0]["adapter"] == "text"
    assert bound[0]["representation_sha256"] == hashlib.sha256(b"Hello custom").hexdigest()


def test_source_provenance_rejects_drift(tmp_path):
    source = _html(tmp_path / "book-1.html")
    expected = source_provenance([source])
    actual = [dict(expected[0], representation_sha256="0" * 64)]
    assert source_provenance_errors(actual, expected) == ["packet source provenance mismatch for book-1.html"]


def test_reused_packet_requires_current_input_attestation(tmp_path):
    source = _html(tmp_path / "book-1.html")
    packet = {"parsed": {"claims": [{"claim": "grounded"}]}, "source_provenance": source_provenance([source])}
    assert source_provenance_errors(packet["source_provenance"], source_provenance([source])) == []
    source.write_text("<p>changed</p>", encoding="utf-8")
    assert source_provenance_errors(packet["source_provenance"], source_provenance([source]))
    assert source_provenance_errors(None, source_provenance([source])) == ["packet source provenance missing"]


def test_workflow_reuse_boundary_recollects_unattested_or_stale_packets(tmp_path):
    source = _html(tmp_path / "book-1.html")
    expected = source_provenance([source])

    workflow = ast.parse((ROOT / "workflows" / "research.py").read_text(encoding="utf-8"))
    reuse_function = next(
        node for node in workflow.body if isinstance(node, ast.FunctionDef) and node.name == "_reuse_evidence_packets"
    )
    namespace = {"json": json, "shutil": shutil, "packet_source_defects": packet_source_defects}
    exec(compile(ast.Module(body=[reuse_function], type_ignores=[]), "workflows/research.py", "exec"), namespace)

    valid = {"parsed": {"claims": ["ok"]}, "source_provenance": expected}
    stale = {"parsed": {"claims": ["ok"]}, "source_provenance": [dict(expected[0], sha256="0" * 64)]}
    unattested = {"parsed": {"claims": ["ok"]}}

    def validator(parsed):
        return [] if parsed and parsed.get("claims") else ["invalid"]

    reuse_dir = tmp_path / "reuse"
    destination_dir = tmp_path / "destination"
    reuse_dir.mkdir()
    destination_dir.mkdir()
    (reuse_dir / "worker-book-1.json").write_text(json.dumps(valid), encoding="utf-8")
    (reuse_dir / "worker-book-2.json").write_text(json.dumps(stale), encoding="utf-8")
    (reuse_dir / "worker-book-3.json").write_text(json.dumps(unattested), encoding="utf-8")
    specs = [("book-1", ""), ("book-2", ""), ("book-3", ""), ("book-4", "")]
    reused = namespace["_reuse_evidence_packets"](
        reuse_dir,
        destination_dir,
        specs,
        {sid: expected for sid, _ in specs},
        validator,
    )
    assert reused == ["book-1"]
    assert [sid for sid, _ in specs if sid not in reused] == ["book-2", "book-3", "book-4"]
    assert (destination_dir / "worker-book-1.json").is_file()
    assert not (destination_dir / "worker-book-2.json").exists()


