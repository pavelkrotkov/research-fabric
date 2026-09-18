"""Visible source text reaches the authoritative gate without a second HTML parse."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import zipfile

import pytest

from research_fabric.sources import bind_manifest, representation_for

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bin.excerpt_grounding import grounded  # noqa: E402


def _epub(path, xhtml):
    with zipfile.ZipFile(ROOT / "tests/fixtures/minimal.epub") as original, zipfile.ZipFile(path, "w") as output:
        for member in original.infolist():
            data = xhtml.encode() if member.filename == "OEBPS/ch1.xhtml" else original.read(member)
            output.writestr(member, data)


def _gate(root, source, good, bad, *, legacy=False):
    rep = representation_for(source)
    row = {"source_id": "s", "snapshot": source.name, "sha256": rep.original_sha256}
    if not legacy:
        row = bind_manifest([source], [row])[0]
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "sources.jsonl").write_text(json.dumps(row) + "\n")
    for excerpts, failures in ((good, 0), (good + bad, len(bad))):
        claims = [{"claim_id": str(i), "source_ids": ["s"], "excerpt": text} for i, text in enumerate(excerpts)]
        (evidence / "claims.jsonl").write_text("\n".join(map(json.dumps, claims)))
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/excerpt_grounding.py"), str(root)], capture_output=True, text=True
        )
        assert result.returncode == bool(failures), result.stderr
        report = json.loads((result.stderr if failures else result.stdout).splitlines()[-1])
        assert report["ungrounded"] == failures


@pytest.mark.parametrize("kind", ["html", "legacy-html", "md", "epub"])
def test_visible_angles_and_marker_shapes_through_gate(tmp_path, kind):
    head = "This sufficiently long opening passage has precise words"
    tail = "and the closing passage also contains enough words"
    visible = f"alpha <five> omega. {head} <five> {tail}. left <!--@@formula--> right."
    source = tmp_path / ("source.html" if kind == "legacy-html" else f"source.{kind}")
    xhtml = "<p>" + visible.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</p>"
    if kind == "epub":
        _epub(source, xhtml)
    else:
        source.write_text(visible if kind == "md" else xhtml)
    _gate(
        tmp_path,
        source,
        ["alpha <five> omega", f"{head} <five> {tail}", "left <!--@@formula--> right"],
        ["alpha omega", "alpha <different> omega", f"{head} {tail}", "left right"],
        legacy=kind == "legacy-html",
    )


@pytest.mark.parametrize(
    "ruby,quote",
    [
        ("<ruby>12<rt>34</rt></ruby>", "12 34"),
        ("<ruby>12<rtc><rt>34</rt></rtc></ruby>", "12 34"),
        ("<ruby>12<rp>(</rp><rt>34</rt><rp>)</rp></ruby>", "12 ( 34 )"),
    ],
)
def test_ruby_boundaries_and_version_drift(tmp_path, ruby, quote):
    source = tmp_path / "source.epub"
    _epub(source, f"<p>{ruby}</p>")
    rep = representation_for(source)
    assert grounded(quote, rep.text)
    assert not grounded("1234", rep.text)
    _gate(tmp_path, source, [quote], ["1234"])
    row = bind_manifest([source], [{"snapshot": source.name, "sha256": rep.original_sha256}])[0]
    assert row["adapter_version"] == "2"
    row["adapter_version"] = "1"
    with pytest.raises(RuntimeError, match="representation drift"):
        bind_manifest([source], [row])
    (tmp_path / "evidence/sources.jsonl").write_text(json.dumps(row) + "\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin/provenance_validate.py"), str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "adapter_version mismatch" in result.stderr


def test_epub_cannot_forge_markers_across_inline_nodes(tmp_path):
    source = tmp_path / "source.epub"
    _epub(source, "<p>left <span>&lt;</span>!--@@formula--&gt; right</p>")
    _gate(tmp_path, source, ["left <!--@@formula--> right"], ["left right"])
