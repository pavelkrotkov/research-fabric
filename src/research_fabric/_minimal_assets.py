"""Byte-preserving local figures for Small; no rendering or image interpretation."""

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from ._openkb045 import CompilationError
from ._source_adapter import _VisualHTML, visual_references


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def references(source):
    text = source.read_text(encoding="utf-8")
    if source.suffix == ".md":
        return visual_references(text)
    if source.suffix == ".html":
        parser = _VisualHTML()
        parser.feed(text)
        return parser.references
    return []


def local_image(target):
    parsed = urlsplit(target)
    name = unquote(parsed.path)
    path = PurePosixPath(name)
    if (
        parsed.scheme
        or parsed.netloc
        or path.is_absolute()
        or not name
        or any(part in ("..", ".git") for part in path.parts)
        or "\\" in name
        or any(ord(char) < 32 for char in name)
        or path.suffix.lower()
        not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".tif", ".tiff", ".pdf", ".eps")
    ):
        raise CompilationError(
            "Figures require local relative image paths; remote/embedded/escaping images unsupported"
        )
    return path.as_posix()


def safe_file(root, name):
    path = root / name
    if (
        ".." in PurePosixPath(name).parts
        or not path.resolve().is_relative_to(root.resolve())
        or any(part.is_symlink() for part in [path, *path.parents] if part.is_relative_to(root))
    ):
        raise CompilationError("Asset path escapes its root or uses a symlink")
    return path


def verify_files(root, expected):
    for name, sha in expected.items():
        path = safe_file(root, name)
        if not path.is_file() or digest(path) != sha:
            raise CompilationError(f"Preserved asset hash mismatch: {name}")


def input_assets(source, row):
    """Require the exact active local asset closure in the source manifest."""
    found = set()
    for reference in references(source):
        if reference["syntax"] == "srcset":
            raise CompilationError("Figure srcset is not qualified; use one explicit local image")
        found.add(local_image(reference["target"]))
    expected = row.get("assets", {})
    if not isinstance(expected, dict) or set(expected) != found:
        raise CompilationError("Manifest assets must map every referenced local image path to its SHA-256")
    verify_files(source.parent, expected)
    return expected


def published_assets(kb, source, row, doc_name, *, copy=False):
    """Preserve raw-relative closure and verify each native source-page image."""
    assets = input_assets(source, row)
    for path in (kb / "wiki/sources/images" / doc_name).rglob("*"):
        if path.is_file() and digest(safe_file(kb, path.relative_to(kb).as_posix())) not in assets.values():
            raise CompilationError("Native conversion copied an undeclared image asset")
    if not assets:
        return {}
    expected = {f"raw/{doc_name}{source.suffix}": row["sha256"]}
    for name, sha in assets.items():
        original = safe_file(source.parent, name)
        for prefix in ("raw/", "wiki/sources/"):
            relative = prefix + name
            target = safe_file(kb, relative)
            if copy and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
            expected[relative] = sha
    page = kb / "wiki/sources" / (doc_name + ".md")
    originals, converted = references(source), references(page)
    if len(originals) != len(converted):
        raise CompilationError("Native conversion lost or added image references")
    for original, image in zip(originals, converted):
        name = local_image(image["target"])
        # OpenKB emits wiki-root-relative sources/images paths for copied images.
        native_copy = name != local_image(original["target"]) and name.startswith(f"sources/images/{doc_name}/")
        target = (kb / "wiki" if native_copy else page.parent) / name
        relative = target.relative_to(kb).as_posix()
        expected[relative] = assets[local_image(original["target"])]
        if digest(safe_file(kb, relative)) != expected[relative]:
            raise CompilationError("Converted image link points to different bytes")
    verify_files(kb, expected)
    manifest = {
        "policy": "small-assets-v1",
        "source_sha256": row["sha256"],
        "image_interpretation": "NOT_QUALIFIED",
        "source_page": {"path": page.relative_to(kb / "wiki").as_posix(), "sha256": digest(page)},
        "files": {name.removeprefix("wiki/"): sha for name, sha in expected.items() if name.startswith("wiki/")},
    }
    relative = f"wiki/assets/minimal/{row['sha256']}.json"
    path = safe_file(kb, relative)
    data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    if copy and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if not path.is_file() or path.read_bytes() != data:
        raise CompilationError("Preserved asset manifest differs from verified outputs")
    expected[relative] = digest(path)
    expected[page.relative_to(kb).as_posix()] = digest(page)
    return expected


def export_assets(wiki, docs):
    """Use Small's accepted asset records in the existing browser exporter."""
    files = {}
    for path in (wiki / "assets/minimal").glob("*.json"):
        manifest = json.loads(safe_file(wiki, path.relative_to(wiki).as_posix()).read_text())
        if (
            manifest["policy"] != "small-assets-v1"
            or path.stem != manifest["source_sha256"]
            or manifest["image_interpretation"] != "NOT_QUALIFIED"
        ):
            raise CompilationError("Invalid Small asset manifest")
        page = manifest["source_page"]
        verify_files(wiki, {page["path"]: page["sha256"], **manifest["files"]})
        for name, sha in manifest["files"].items():
            if not name.startswith("sources/"):
                raise CompilationError("Small asset outside source-page namespace")
            target = safe_file(docs, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(safe_file(wiki, name), target)
            verify_files(docs, {name: sha})
            files[name] = sha
        name = path.relative_to(wiki).as_posix()
        target = safe_file(docs, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)
        verify_files(docs, {name: digest(path)})
        files[name] = digest(path)
    return files
