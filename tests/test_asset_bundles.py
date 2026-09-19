"""Real parser, filesystem, renderer and exporter regressions for visual bundles."""

import copy
import json
import pathlib
import shutil

import pytest
from PIL import Image

from research_fabric import _source_assets as assets
from research_fabric._source_adapter import visual_references
from research_fabric.sources import (
    copy_source_snapshot,
    prepare_source_bundle,
    representation_for,
    source_attestation,
)


def png(path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def prepare(source, destination):
    return prepare_source_bundle(source, destination, source_attestation(representation_for(source)))


def test_parsed_discovery_ignores_examples_and_preserves_reference_identity(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure one.png")
    source.write_text(
        "# Heading\n\n![Caption][plot]\n\n[plot]: <figure one.png#detail>\n\n"
        '<img src="figure%20one.png" alt="HTML caption">\n\n'
        '<!-- <img src="missing.png"> -->\n\n'
        "`![example](missing-inline.png)`\n\n"
        '```md\n<img src="missing-code.png">\n![example](missing.png)\n```\n'
    )
    original = source.read_bytes()
    rows = visual_references(original.decode())
    assert len(rows) == 2
    assert rows[0]["caption"] == "Caption"
    assert rows[1]["caption"] == "HTML caption"
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    prepared = (root / manifest["input_path"]).read_text()
    assert "Caption" in prepared and "HTML caption" in prepared
    assert "![example]" not in prepared
    assert len(manifest["assets"]) == 2
    assert manifest["assets"][0]["original_path"] == "original/figure one.png"
    assert manifest["assets"][0]["target"].endswith("#detail")
    assert len({row["derivative_path"] for row in manifest["assets"]}) == 1
    assets.verify_bundle(root, manifest)
    assert source.read_bytes() == original
    assert manifest == prepare(source, tmp_path / "bundles")


def test_same_document_and_basename_different_work_assets_do_not_collide(tmp_path):
    results = []
    snapshots = tmp_path / "snapshots"
    for work, color in (("a", "red"), ("b", "blue")):
        directory = tmp_path / work
        png(directory / "figure.png", color)
        source = directory / "paper.md"
        source.write_text("![Figure](figure.png)\n")
        results.append(prepare(source, tmp_path / "bundles"))
        copied = copy_source_snapshot(source, snapshots)
        assert copied.read_bytes() == source.read_bytes()
        assert (copied.parent / "figure.png").read_bytes() == (directory / "figure.png").read_bytes()
    assert results[0]["key"] != results[1]["key"]
    assert results[0]["input_path"] != results[1]["input_path"]


@pytest.mark.parametrize("target", ["../escape.png", "/absolute.png", "%2e%2e/escape.png", "escape.png"])
def test_unsafe_or_missing_asset_fails_before_preparation(tmp_path, target):
    source = tmp_path / "paper.md"
    source.write_text(f"![Figure]({target})")
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare(source, tmp_path / "bundles")


def test_symlink_escape_rejected(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    png(tmp_path / "outside.png")
    (directory / "figure.png").symlink_to(tmp_path / "outside.png")
    source = directory / "paper.md"
    source.write_text("![Figure](figure.png)")
    with pytest.raises(ValueError, match="unsafe"):
        prepare(source, tmp_path / "bundles")


def test_remote_and_unsupported_are_explicit_never_downloaded(tmp_path):
    source = tmp_path / "paper.md"
    source.write_text('<img src="https://invalid.example/image.png" data-optional="true">')
    manifest = prepare(source, tmp_path / "bundles")
    assert manifest["assets"][0]["limitation"] == "remote-not-fetched"
    source.write_text('<img src="https://invalid.example/image.png">')
    with pytest.raises(ValueError, match="remote visual"):
        prepare(source, tmp_path / "bundles")
    source.write_text('<img srcset="one.png 1x, two.png 2x">')
    with pytest.raises(ValueError, match="srcset"):
        prepare(source, tmp_path / "bundles")


def test_optional_unsupported_preserves_original_link(tmp_path):
    source = tmp_path / "paper.md"
    (tmp_path / "image.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    source.write_text('<object data="image.svg" data-optional="true"></object>')
    manifest = prepare(source, tmp_path / "bundles")
    row = manifest["assets"][0]
    assert (tmp_path / "bundles" / manifest["key"] / row["original_path"]).is_file()
    assert "unsupported visual format" in row["limitation"]
    assert "derivative_path" not in row
    prepared = (tmp_path / "bundles" / manifest["key"] / manifest["input_path"]).read_text()
    assert "original/image.svg" in prepared


@pytest.mark.parametrize("suffix,renderer", [(".pdf", "pdftoppm"), (".eps", "gs")])
def test_real_bounded_render_and_resume_drift(tmp_path, suffix, renderer):
    if not shutil.which(renderer):
        pytest.skip(f"requires {renderer}")
    image = tmp_path / ("figure" + suffix)
    if suffix == ".pdf":
        Image.new("RGB", (50, 30), "blue").save(image, "PDF")
    else:
        image.write_text(
            "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 50 30\n1 0 0 setrgbcolor\n0 0 50 30 rectfill\nshowpage\n"
        )
    source = tmp_path / "paper.md"
    source.write_text(f'<embed src="{image.name}">')
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    row = manifest["assets"][0]
    assert row["renderer"]["name"] == renderer
    assert row["original_sha256"] == assets.digest(image.read_bytes())
    with Image.open(root / row["derivative_path"]) as raster:
        raster.load()
        assert max(raster.size) <= 1600
    assets.verify_bundle(root, manifest)
    changed = copy.deepcopy(manifest)
    changed["assets"][0]["renderer"]["version"] = "different"
    with pytest.raises(ValueError, match="version/configuration drift"):
        assets.verify_bundle(root, changed)
    (root / row["derivative_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="drift"):
        assets.verify_bundle(root, manifest)
    with pytest.raises(ValueError, match="drift"):
        prepare(source, tmp_path / "bundles")


def test_missing_renderer_and_malformed_required_visual_fail(tmp_path, monkeypatch):
    source = tmp_path / "paper.md"
    source.write_text('<embed src="figure.pdf">')
    (tmp_path / "figure.pdf").write_bytes(b"not a PDF")
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(ValueError, match="renderer unavailable"):
            prepare(source, tmp_path / "bundles")
    if shutil.which("pdftoppm"):
        with pytest.raises(ValueError, match="unreadable"):
            prepare(source, tmp_path / "bundles")


def test_original_and_policy_drift_fail(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text("![Figure](figure.png)")
    attestation = source_attestation(representation_for(source))
    manifest = prepare_source_bundle(source, tmp_path / "bundles", attestation)
    root = tmp_path / "bundles" / manifest["key"]
    changed = copy.deepcopy(manifest)
    changed["policy"]["max_pixels"] = 1
    with pytest.raises(ValueError, match="policy drift"):
        assets.verify_bundle(root, changed)
    png(tmp_path / "figure.png", "blue")
    with pytest.raises(ValueError, match="attestation drift"):
        prepare_source_bundle(source, tmp_path / "bundles", attestation)
    (root / manifest["assets"][0]["original_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="drift"):
        assets.verify_bundle(root, manifest)


@pytest.mark.parametrize("field,value", [("key", "wrong"), ("input_path", "prepared/wrong.md")])
def test_exporter_only_copies_manifest_closure_and_rejects_drift(tmp_path, field, value):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text("![Figure](figure.png)")
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    wiki = tmp_path / "wiki"
    row = manifest["assets"][0]
    native = wiki / "sources/images" / manifest["key"] / pathlib.Path(row["derivative_path"]).name
    native.parent.mkdir(parents=True)
    native.write_bytes((root / row["derivative_path"]).read_bytes())
    (native.parent / "unreferenced.png").write_bytes(b"must not export")
    assets.publish_bundle(root, manifest, wiki, manifest["key"])
    published = wiki / "assets" / manifest["key"] / "bundle.json"
    assert published.read_bytes() == (root / "bundle.json").read_bytes()
    changed = copy.deepcopy(manifest)
    changed["assets"] = []
    (root / "bundle.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="manifest drift"):
        assets.publish_bundle(root, manifest, wiki, manifest["key"])
    docs = tmp_path / "docs"
    copied = assets.export_assets(wiki, docs)
    assert len(copied) == 2
    assert (docs / published.relative_to(wiki)).read_bytes() == published.read_bytes()
    assert not list(docs.rglob("unreferenced.png"))
    native.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="drift"):
        assets.export_assets(wiki, tmp_path / "fresh-docs")
    receipt = next(wiki.glob("assets/*/bundle.json"))
    data = json.loads(receipt.read_text())
    data[field] = value
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity"):
        assets.export_assets(wiki, tmp_path / "fresh-docs")


@pytest.mark.parametrize(
    "body",
    [
        '<pre><code><img src="missing.png"></code></pre>',
        '<script>const example = `<img src="missing.png">`;</script>',
        '<style>p:after {content: "<img src=missing.png>"}</style>',
        'Inline <code><img src="missing.png"></code> example',
        "Inline <code>![example](missing.png)</code> example",
    ],
)
def test_raw_html_code_examples_are_inert(tmp_path, body):
    source = tmp_path / "paper.md"
    source.write_text(body + "\n\n![Real](real.png)")
    png(tmp_path / "real.png")
    manifest = prepare(source, tmp_path / "bundles")
    assert [row["target"] for row in manifest["assets"]] == ["real.png"]


def test_html_rewrite_shares_discovery_inert_and_object_policy(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    body = '<object data="figure.png">outer <object>fallback</object> <code><img src="missing.png"></code></object>'
    source.write_text(body)
    manifest = prepare(source, tmp_path / "bundles")
    prepared = (tmp_path / "bundles" / manifest["key"] / manifest["input_path"]).read_text()
    assert [row["target"] for row in manifest["assets"]] == ["figure.png"]
    assert prepared.startswith(
        '[Figure]outer <object>fallback</object> <code><img src="missing.png"></code>\n\n## Source figures'
    )


def test_page_selection_and_caption_cannot_change_native_input_scope(tmp_path):
    source = tmp_path / "paper.md"
    (tmp_path / "figure.pdf").write_bytes(b"original")
    source.write_text('<embed src="figure.pdf#page=2">')
    with pytest.raises(ValueError, match="first vector page"):
        prepare(source, tmp_path / "bundles")
    png(tmp_path / "figure.png")
    source.write_text('<img src="figure.png" alt="![injected](secret.png)">')
    manifest = prepare(source, tmp_path / "bundles")
    prepared = (tmp_path / "bundles" / manifest["key"] / manifest["input_path"]).read_text()
    assert "![injected]" not in prepared


def test_deleted_frozen_derivative_is_not_silently_regenerated(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text("![Figure](figure.png)")
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    (root / manifest["assets"][0]["derivative_path"]).unlink()
    with pytest.raises(FileNotFoundError):
        prepare(source, tmp_path / "bundles")


def test_snapshot_output_symlink_escape_fails(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text("![Figure](figure.png)")
    manifest = prepare(source, tmp_path / "bundles")
    outside = tmp_path / "outside"
    outside.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / manifest["key"]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        copy_source_snapshot(source, snapshots)
    assert list(outside.iterdir()) == []


def test_bundle_cannot_drop_parsed_references_on_resume(tmp_path):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text("![Figure](figure.png)")
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    manifest["assets"] = []
    (root / "bundle.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="reference mapping drift"):
        prepare(source, tmp_path / "bundles")


SCIENCE = (pathlib.Path(__file__).parent / "fixtures/scientific_text.md").read_text()


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("figures", ["", "\n![Plot][figure]\n\n[figure]: figure.png\n"])
def test_prepared_input_preserves_scientific_source_outside_visuals(tmp_path, ending, figures):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    text = (SCIENCE + figures).replace("\n", ending)
    source.write_bytes(text.encode())
    manifest = prepare(source, tmp_path / "bundles")
    prepared = (tmp_path / "bundles" / manifest["key"] / manifest["input_path"]).read_bytes()
    assert prepared.startswith(SCIENCE.replace("\n", ending).encode())
    if not figures:
        assert prepared == source.read_bytes()
    assets.verify_bundle(tmp_path / "bundles" / manifest["key"], manifest)


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_reference_edits_preserve_container_text_and_unrelated_html(tmp_path, ending):
    source = tmp_path / "paper.md"
    png(tmp_path / "figure.png")
    source.write_text(
        "> **Math** $n!$\n> ![First\n> caption](figure.png)\n> $a \\\\ b$\n\n"
        "- Text with `![fake](missing.png)` and ![Second](figure.png).\n\n"
        '<div class="original"><img src="figure.png" alt="Third">$n!$</div>\n\n'
        'Inline <code><img src="missing.png"></code> untouched.\n'
    )
    source.write_bytes(source.read_text().replace("\n", ending).encode())
    manifest = prepare(source, tmp_path / "bundles")
    prepared = (tmp_path / "bundles" / manifest["key"] / manifest["input_path"]).read_text()
    assert "> **Math** $n!$\n> [Figure]\n> $a \\\\ b$" in prepared
    assert "- Text with `&#33;[fake](missing.png)` and [Figure]." in prepared
    assert '<div class="original">[Figure]$n!$</div>' in prepared
    assert 'Inline <code><img src="missing.png"></code> untouched.' in prepared
    assert len(manifest["assets"]) == 3
