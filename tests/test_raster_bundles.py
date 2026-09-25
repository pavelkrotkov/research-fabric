"""Bound decoding separately from delivery; availability is not visual review."""

import copy
import io
import json
import struct
import zlib

import pytest
from PIL import Image

from research_fabric import _source_assets as assets
from research_fabric.reading import prepare_reading_plan
from research_fabric.sources import prepare_source_bundle, representation_for, source_attestation


def fixture(tmp_path, size=(6000, 3000)):
    image = tmp_path / "plot.png"
    Image.new("RGB", size, "white").save(image)
    source = tmp_path / "paper.md"
    source.write_text("# Text\n\nText evidence.\n\n# Visual\n\n![Equation](plot.png)\n")
    return source, image


def prepare(source, destination):
    return prepare_source_bundle(source, destination, source_attestation(representation_for(source)))


@pytest.mark.parametrize("size", [(16, 16), (6000, 3000)])
def test_bounded_raster_original_identity_and_frozen_resume(tmp_path, monkeypatch, size):
    source, image = fixture(tmp_path, size)
    original = image.read_bytes()
    manifest = prepare(source, tmp_path / "bundles")
    root = tmp_path / "bundles" / manifest["key"]
    row = manifest["assets"][0]
    assert row["locator"]["lines"] == [7, 8]
    assert row["original_sha256"] == assets.digest(original)
    assert (root / row["original_path"]).read_bytes() == original == image.read_bytes()
    derivative = (root / row["derivative_path"]).read_bytes()
    assert row["derivative_sha256"] == assets.digest(derivative)
    with Image.open(io.BytesIO(derivative)) as output:
        assert output.size == ((16, 16) if size == (16, 16) else (1600, 800))
    assert row["renderer"]["original_dimensions"] == list(size)
    assert row["renderer"]["operation"] == ("byte-exact-copy" if size == (16, 16) else "downsample")
    assert (derivative == original) == (size == (16, 16))
    assert manifest["visual_disposition"] == "available-uninspected"
    assert "mathematical content unverified" in (root / manifest["input_path"]).read_text()
    monkeypatch.setattr(assets, "_prepare_raster", lambda *_: pytest.fail("resume decoded the raster"))
    assert prepare(source, tmp_path / "bundles") == manifest
    changed = copy.deepcopy(manifest)
    changed["assets"][0]["renderer"]["version"] = "incompatible"
    with pytest.raises(ValueError, match="version/configuration drift"):
        assets.verify_bundle(root, changed)
    changed = copy.deepcopy(manifest)
    changed["policy"]["raster"]["max_input_pixels"] += 1
    with pytest.raises(ValueError, match="policy drift"):
        assets.verify_bundle(root, changed)
    (root / row["derivative_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="drift"):
        prepare(source, tmp_path / "bundles")


def header_png(width, height):
    # Valid header/CRC, no pixel allocation: the limit must fire before decoding.
    header = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + header + struct.pack(">I", zlib.crc32(header)) + b"\0\0\0\0IDAT"
    )


@pytest.mark.parametrize("size", [(8000, 5000), (16001, 1), (10000, 10000), (100000, 100000)])
def test_extreme_headers_fail_before_pixel_decode(tmp_path, monkeypatch, size):
    source, image = fixture(tmp_path, (16, 16))
    image.write_bytes(header_png(*size))
    monkeypatch.setattr(Image.Image, "load", lambda *_: pytest.fail("unbounded pixel decode"))
    with pytest.raises(ValueError, match="required visual unreadable.*dependent readings blocked"):
        prepare(source, tmp_path / "bundles")
    assert not list((tmp_path / "bundles").rglob("bundle.json"))


def test_malformed_truncated_animated_and_byte_limit(tmp_path):
    source, image = fixture(tmp_path, (16, 16))
    valid = image.read_bytes()
    frames = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(frames, "GIF", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")])
    for data in (b"not an image", valid[:40], frames.getvalue()):
        image.write_bytes(data)
        with pytest.raises(ValueError, match="required visual unreadable"):
            prepare(source, tmp_path / "bundles")
    with image.open("wb") as stream:
        stream.truncate(assets.MAX_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds.*bytes"):
        prepare(source, tmp_path / "bundles")


@pytest.mark.parametrize("high_bit_depth", [False, True])
def test_required_block_and_optional_reading_dispositions(tmp_path, high_bit_depth):
    source, image = fixture(tmp_path, (16, 16))
    if high_bit_depth:
        contrast = Image.new("I;16", (2000, 1000), 1000)
        contrast.paste(Image.new("I;16", (1000, 1000), 50000), (1000, 0))
        contrast.save(image)
        with Image.open(image) as original:
            assert original.getextrema() == (1000, 50000)
    else:
        image.write_bytes(b"unreadable")
    original_bytes = image.read_bytes()
    profile = {
        "reading": {
            "sources": {"paper.md": "paper"},
            "works": {"paper": {"title": None, "authors": None}},
            "policy": {"target_tokens": 1},
        }
    }

    def plan():
        rows = [{**source_attestation(representation_for(source)), "source_id": "paper", "snapshot": "paper.md"}]
        return prepare_reading_plan(tmp_path, profile, rows, tmp_path / "plan.json", tmp_path / "bundles")

    with pytest.raises(ValueError, match=r"paper.md: plot.png.*lines.*7, 8.*source incomplete"):
        plan()
    assert not (tmp_path / "plan.json").exists()
    suffix = "png" if high_bit_depth else "svg"
    image.rename(tmp_path / f"plot.{suffix}")
    source.write_text(
        source.read_text().replace("![Equation](plot.png)", f'<img src="plot.{suffix}" data-optional="true">')
    )
    result, bundles = plan()
    assert bundles["paper.md"]["visual_disposition"] == "incomplete-optional-visuals"
    assert result.data["sources"]["paper.md"]["visual_disposition"] == "incomplete-optional-visuals"
    text, visual = result.data["readings"]
    assert text["visual_disposition"] == "no-visuals"
    assert visual["visual_disposition"] == "incomplete-optional-visuals"
    assert visual["assets"] == [{"source_file": "paper.md", "index": 0}]
    bundle = bundles["paper.md"]
    row = bundle["assets"][0]
    reason = "unsupported raster mode for downsampling" if high_bit_depth else "unsupported visual format"
    assert reason in row["limitation"]
    assert "derivative_path" not in row
    assert (tmp_path / "bundles" / bundle["key"] / row["original_path"]).read_bytes() == original_bytes
    assert plan()[0].data == result.data
    changed = json.loads((tmp_path / "plan.json").read_text())
    changed["readings"][1]["visual_disposition"] = "complete"
    (tmp_path / "plan.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="visual assignment/disposition drift"):
        plan()
