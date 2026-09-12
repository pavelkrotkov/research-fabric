"""EPUB adapter acceptance tests."""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from research_fabric._epub_adapter import _chapter
from research_fabric.sources import adapter_for, bind_manifest, representation_for, source_bundle

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _row(source: pathlib.Path) -> dict:
    rep = representation_for(source)
    row = {
        "snapshot": source.name,
        "sha256": rep.original_sha256,
        "adapter": rep.adapter,
        "adapter_version": rep.adapter_version,
        "representation_encoding": "utf-8",
        "representation_sha256": rep.representation_sha256,
    }
    if rep.source_metadata:
        row["source_metadata"] = dict(rep.source_metadata)
    return row


def test_epub_golden_representation_and_metadata():
    source = FIXTURES / "minimal.epub"
    expected = (FIXTURES / "minimal.epub.txt").read_text(encoding="utf-8").rstrip("\n")
    first = representation_for(source)
    second = representation_for(source)
    assert first.text == second.text == expected
    assert first.representation_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert "@@section OEBPS/ch1.xhtml#intro" in first.text
    assert "@@formula x=1 omega." in first.text
    assert "@@asset OEBPS/images/chart.png" in first.text
    assert source_bundle(source) == ((source, pathlib.Path("minimal.epub")),)
    assert adapter_for(source).map_locator(" OEBPS/ch1.xhtml#intro ") == "OEBPS/ch1.xhtml#intro"
    bound = bind_manifest([source], [{"snapshot": source.name, "sha256": first.original_sha256}])[0]
    assert bound["source_metadata"] == {
        "identifier": "urn:test:golden",
        "language": "en",
        "package_path": "OEBPS/package.opf",
        "spine_items": "2",
        "title": "Golden EPUB",
        "version": "3.0",
    }


def test_epub_spine_reorder_is_representation_drift(tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes((FIXTURES / "minimal.epub").read_bytes())
    row = _row(source)
    source.write_bytes((FIXTURES / "reordered.epub").read_bytes())
    row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="representation drift"):
        bind_manifest([source], [row])


@pytest.mark.parametrize(
    "name,error,message",
    [
        ("malformed.epub", ValueError, "malformed EPUB archive"),
        ("missing-resource.epub", FileNotFoundError, "missing EPUB resource"),
        ("duplicate-content.epub", ValueError, "duplicate EPUB spine content"),
        ("dtd.epub", ValueError, "DTD/entity declarations forbidden"),
        ("deep.epub", ValueError, "XML nesting exceeds"),
        ("zip-bomb.epub", ValueError, "unsafe EPUB compression ratio"),
    ],
)
def test_epub_invalid_inputs_fail_clearly(name, error, message):
    with pytest.raises(error, match=message):
        representation_for(FIXTURES / name)


def test_epub_table_cells_do_not_merge():
    xhtml = b'<html><body><table><tr><td>12</td><td>34</td></tr></table></body></html>'
    text = _chapter("OEBPS/ch.xhtml", xhtml, set())
    assert "1234" not in text
    assert "12\n\n34" in text
