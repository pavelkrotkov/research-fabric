"""Offline real OpenKB 0.4.5 conversion → existing browser exporter.

Run with OpenKB 0.4.5, Pillow, pdftoppm, Ghostscript and this package installed.
No model calls, compiler replacement, fake conversion, or original mutations.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
from urllib.parse import unquote

from markdown_it import MarkdownIt
from PIL import Image

from research_fabric._source_assets import digest, publish_bundle
from research_fabric.sources import prepare_source_bundle, representation_for, source_attestation

ENGINE = pathlib.Path(__file__).resolve().parents[1]
SCIENCE = (ENGINE / "tests/fixtures/scientific_text.md").read_text()
CAPTION = r"$n!$ and $$\begin{aligned} a &= b \\ c &= d \end{aligned}$$"

with tempfile.TemporaryDirectory(prefix="native-assets-") as directory:
    root = pathlib.Path(directory)
    os.chdir(root)
    # Native imports can create local runtime artifacts; keep them isolated too.
    import openkb.config as config
    from click.testing import CliRunner
    from openkb.cli import cli
    from openkb.converter import convert_document

    config.GLOBAL_CONFIG_DIR = root / "global"
    config.GLOBAL_CONFIG_PATH = root / "global/global.yaml"
    config.GLOBAL_CONFIG_LOCK_PATH = root / "global/global.lock"
    result = CliRunner().invoke(cli, ["init", "--model", "openai/test", "--language", "en"], input="\n")
    assert result.exit_code == 0, (result.output, repr(result.exception))
    for work, color in (("first", "red"), ("second", "blue"), ("text-only", "green")):
        originals = root / work
        originals.mkdir()
        Image.new("RGB", (40, 30), color).save(originals / "figure one.png")
        Image.new("RGB", (40, 30), color).save(originals / "figure.pdf", "PDF")
        (originals / "figure.eps").write_text(
            "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 50 30\n0 0 50 30 rectfill\nshowpage\n"
        )
        source = originals / "paper.md"
        source.write_text(
            SCIENCE + "# Experiment\n\nThe measurement is $x = 1$.\n\n![Caption][plot]\n\n"
            '[plot]: <figure one.png#detail>\n\n<img src="figure%20one.png" alt="Second caption">\n\n'
            '<embed src="figure.pdf">\n\n<object data="figure.eps"></object>\n\n'
            "<!-- ![example](missing.png) -->\n\n`![example](missing-inline.png)`\n\n"
            "```md\n![example](missing-fenced.png)\n```\n"
        )
        source.write_text(source.read_text().replace("Second caption", CAPTION))
        if work == "text-only":
            source.write_text(SCIENCE)
        before = {path.name: digest(path.read_bytes()) for path in originals.iterdir()}
        manifest = prepare_source_bundle(source, root / "bundles", source_attestation(representation_for(source)))
        bundle_root = root / "bundles" / manifest["key"]
        converted = convert_document(bundle_root / manifest["input_path"], root)
        assert converted.doc_name == manifest["key"]
        assert converted.source_path.is_file()
        publish_bundle(bundle_root, manifest, root / "wiki", converted.doc_name)
        assert before == {path.name: digest(path.read_bytes()) for path in originals.iterdir()}
        assert SCIENCE in converted.source_path.read_text()
        if work != "text-only":
            assert CAPTION in converted.source_path.read_text()
        if work == "text-only":
            assert not manifest["assets"]
        for row in manifest["assets"]:
            copied = root / "wiki/sources/images" / converted.doc_name / pathlib.Path(row["derivative_path"]).name
            assert digest(copied.read_bytes()) == row["derivative_sha256"]
            with Image.open(copied) as image:
                image.load()  # Native read-image consumes the same raster formats.
    html_source = root / "chapter.html"
    html_source.write_text("<h1>HTML chapter</h1><p>Original HTML evidence.</p>")
    manifest = prepare_source_bundle(html_source, root / "bundles", source_attestation(representation_for(html_source)))
    bundle_root = root / "bundles" / manifest["key"]
    converted = convert_document(bundle_root / manifest["input_path"], root)
    assert converted.doc_name == "chapter"
    publish_bundle(bundle_root, manifest, root / "wiki", converted.doc_name)
    docs = root / "site/docs"
    subprocess.run(
        [sys.executable, str(ENGINE / "tools/wiki/build_wiki.py"), "--vault", str(root / "wiki"), "--docs", str(docs)],
        check=True,
    )
    pages = list((docs / "sources").glob("*.md"))
    assert len(pages) == 4
    for page in pages:
        if page.stem == "chapter":
            assert "Original HTML evidence." in page.read_text()
            continue
        assert SCIENCE in page.read_text()
        if "# Experiment" in page.read_text():
            assert CAPTION in page.read_text()
        links = []
        for token in MarkdownIt("commonmark").parse(page.read_text()):
            for child in token.children or ():
                if child.type == "image":
                    links.append(child.attrGet("src"))
                elif child.type == "link_open":
                    links.append(child.attrGet("href"))
        assert len(links) in (0, 8), links
        if not links:
            assert "# Experiment" not in page.read_text()
        for link in links:
            assert (page.parent / unquote(link)).resolve().is_file(), (page, link)
    assert not list(docs.rglob("missing*"))
    print("PASS: colliding works, real PDF/EPS rendering, native conversion/exporter; all links/hashes intact")
