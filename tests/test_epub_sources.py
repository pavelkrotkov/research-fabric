"""EPUB adapter acceptance tests."""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

from research_fabric._epub_adapter import _chapter, _member
from research_fabric.sources import adapter_for, bind_manifest, representation_for, source_bundle

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bin.excerpt_grounding import grounded  # noqa: E402

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
    assert "<!--@@section OEBPS/ch1.xhtml#intro-->" in first.text
    assert "<!--@@formula--> x=1 omega." in first.text
    assert "<!--@@asset OEBPS/images/chart.png-->" in first.text
    assert grounded("x=1 omega", first.text) and not grounded("formula x=1", first.text)
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


def test_epub_block_boundaries_do_not_merge():
    xhtml = (
        b"<html><body><table><tr><td>12</td><td>34</td></tr></table>"
        b"<dl><dt>56</dt><dd>78</dd></dl><select><option>90</option><option>12</option></select></body></html>"
    )
    text = _chapter("OEBPS/ch.xhtml", xhtml, set())
    assert all(joined not in text for joined in ("1234", "5678", "9012"))


def test_epub_svg_assets_are_bound_without_visual_text():
    xhtml = b'<html><body><svg><text>hidden</text><image href="images/chart.png"/></svg></body></html>'
    text = _chapter("OEBPS/ch.xhtml", xhtml, {"OEBPS/images/chart.png"})
    assert text == "<!--@@asset OEBPS/images/chart.png-->"
    with pytest.raises(FileNotFoundError, match="missing EPUB asset"):
        _chapter("OEBPS/ch.xhtml", xhtml, set())


def test_epub_generated_heading_locators_are_unique_and_not_evidence():
    xhtml = b'<html><body><h1 id="h2">First</h1><h1>Second</h1></body></html>'
    locators = [line for line in _chapter("OEBPS/ch.xhtml", xhtml, set()).splitlines() if "@@section " in line]
    assert locators == ["<!--@@section OEBPS/ch.xhtml#h2-->", "<!--@@section OEBPS/ch.xhtml#_h2-->"]
    with pytest.raises(ValueError, match="duplicate EPUB heading id"):
        _chapter("OEBPS/ch.xhtml", b'<html><body><h1 id="dup">A</h1><h2 id="dup">B</h2></body></html>', set())
    poisoned = _chapter(
        "OEBPS/ch.xhtml",
        b'<html><body><h1 id="x&#10;invented claim">Real heading</h1></body></html>',
        set(),
    )
    assert "%0Ainvented%20claim" in poisoned
    assert grounded("Real heading", poisoned) and not grounded("invented claim", poisoned)


def test_epub_section_locator_components_are_escaped_independently():
    left = _chapter("a#b", b'<html><body><h1 id="c">Left</h1></body></html>', set())
    right = _chapter("a", b'<html><body><h1 id="b#c">Right</h1></body></html>', set())
    assert "<!--@@section a%23b#c-->" in left
    assert "<!--@@section a#b%23c-->" in right


def test_epub_rejects_utf16_dtd():
    xhtml = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE html [<!ENTITY x "INJECTED">]>'
        "<html><body>&x;</body></html>"
    ).encode("utf-16")
    with pytest.raises(ValueError, match="DTD/entity declarations forbidden"):
        _chapter("OEBPS/ch.xhtml", xhtml, set())


@pytest.mark.parametrize("href", [r"\\server\share.png", "%2e%2e%5csecret"])
def test_epub_rejects_backslash_resource_paths(href):
    with pytest.raises(ValueError, match="unsafe EPUB resource"):
        _member("OEBPS/ch.xhtml", href)
