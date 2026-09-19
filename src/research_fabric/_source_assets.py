"""Validated asset closure owned by source preparation, never an image reviewer."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import shutil
import subprocess
import tempfile
from urllib.parse import quote, urlsplit

from PIL import Image

from ._source_adapter import _local_asset, _visual_source, literal_caption

POLICY = {"version": 1, "page": 1, "region": "whole-page", "max_pixels": 1600, "timeout_seconds": 30}
MAX_BYTES = 20_000_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    path = pathlib.PurePosixPath(relative)
    candidate = root.resolve().joinpath(*path.parts).resolve()
    if path.is_absolute() or ".." in path.parts or "\\" in relative or root.resolve() not in candidate.parents:
        raise ValueError(f"unsafe source asset path: {relative}")
    return candidate


def _read(path: pathlib.Path) -> bytes:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"asset exceeds {MAX_BYTES} bytes: {path.name}")
    return path.read_bytes()


def _raster(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("asset exceeds 16 million pixels")
        image.load()


def _renderer(suffix: str) -> tuple[str, dict]:
    name = "pdftoppm" if suffix == ".pdf" else "gs"
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"required renderer unavailable: {name}")
    result = subprocess.run(
        [executable, "-v" if name == "pdftoppm" else "--version"], capture_output=True, timeout=5, check=True
    )
    version = (result.stdout + result.stderr).decode("utf-8", errors="strict").strip()
    return executable, {"name": name, "version": version, "configuration": POLICY}


def _render(data: bytes, suffix: str) -> tuple[bytes, dict]:
    executable, record = _renderer(suffix)
    with tempfile.TemporaryDirectory(prefix="source-render-") as directory:
        root = pathlib.Path(directory)
        source = root / ("input" + suffix)
        source.write_bytes(data)
        if suffix == ".pdf":
            args = ["-f", "1", "-singlefile", "-scale-to", "1600", "-png", str(source), str(root / "output")]
        else:
            args = [
                "-dSAFER",
                "-dBATCH",
                "-dNOPAUSE",
                "-dFirstPage=1",
                "-dLastPage=1",
                "-dFIXEDMEDIA",
                "-dEPSFitPage",
                "-g1600x1600",
                "-r96",
                "-sDEVICE=png16m",
                f"-sOutputFile={root / 'output.png'}",
                str(source),
            ]
        subprocess.run(
            [executable, *args],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=POLICY["timeout_seconds"],
        )
        output = _read(root / "output.png")
        _raster(output)
        return output, record


def _write(root: pathlib.Path, relative: str, data: bytes) -> None:
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError(f"bundle drift: {relative}")
        return
    target.write_bytes(data)


def _derivative(suffix: str, data: bytes, target: str) -> tuple[bytes, str, dict | None]:
    if suffix in (".pdf", ".eps"):
        fragment = urlsplit(target).fragment
        if fragment and fragment != "page=1":
            raise ValueError("only the complete first vector page is supported")
        data, renderer = _render(data, suffix)
        return data, ".png", renderer
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise ValueError(f"unsupported visual format: {suffix}")
    _raster(data)
    return data, suffix, None


def _local_record(source: pathlib.Path, reference: dict, root: pathlib.Path, original_prefix: str) -> dict:
    relative = _local_asset(reference["target"])
    if not relative:
        raise ValueError("empty local visual reference")
    original = safe_path(source.parent, relative)
    data = _read(original)
    original_path = original_prefix + relative
    _write(root, original_path, data)
    record = {
        **reference,
        "original_path": original_path,
        "original_sha256": digest(data),
    }
    try:
        data, suffix, renderer = _derivative(original.suffix.lower(), data, reference["target"])
        output = "prepared/assets/" + digest(data) + suffix
        _write(root, output, data)
        record.update(derivative_path=output, derivative_sha256=digest(data), renderer=renderer)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if reference["required"]:
            raise ValueError(f"required visual unreadable: {relative}: {exc}") from exc
        record["limitation"] = str(exc)
    return record


def _record(source: pathlib.Path, reference: dict, root: pathlib.Path, original_prefix: str) -> dict:
    if reference["syntax"] == "srcset":
        if reference["required"]:
            raise ValueError("unsupported required visual syntax: srcset")
        return {**reference, "limitation": "unsupported-srcset"}
    parsed = urlsplit(reference["target"])
    if parsed.scheme or parsed.netloc:
        if reference["required"]:
            raise ValueError(f"required remote visual unavailable offline: {reference['target']}")
        return {**reference, "limitation": "remote-not-fetched"}
    return _local_record(source, reference, root, original_prefix)


def _derived_text(text: str, records: list[dict], key: str) -> str:
    if not records:
        return text
    gallery = []
    for number, row in enumerate(records, 1):
        gallery.append(f"\nFigure {number}; source locator: {json.dumps(row['locator'], sort_keys=True)}\n")
        gallery.append(literal_caption(row.get("caption", "")) + "\n")
        if row.get("derivative_path"):
            gallery.append(f"![Figure {number}]({row['derivative_path'].removeprefix('prepared/')})\n")
        if row.get("original_path"):
            gallery.append(f"[Original figure {number}](../assets/{key}/{quote(row['original_path'])})\n")
        if row.get("limitation"):
            gallery.append("Visual unavailable:\n\n" + literal_caption(row["limitation"]) + "\n")
    return text + "\n\n## Source figures\n" + "\n".join(gallery)


def prepare_bundle(source: pathlib.Path, destination: pathlib.Path, attestation: dict) -> dict:
    """Freeze a source-scoped original/derivative closure; reject any resume drift."""
    raw = source.read_bytes()
    if digest(raw) != attestation["sha256"]:
        raise ValueError("source changed after manifest validation")
    key = bundle_key(attestation)
    root = destination / key
    root.mkdir(parents=True, exist_ok=True)
    previous = root / "bundle.json"
    if previous.exists():
        manifest = json.loads(previous.read_text(encoding="utf-8"))
        verify_bundle(root, manifest)
        return manifest
    normalized = attestation["adapter"] == "markdown"
    references, text = _visual_source(raw.decode("utf-8")) if normalized else ([], "")
    original_path = "original/" + attestation["source_file"]
    original_prefix = pathlib.PurePosixPath(original_path).parent.as_posix() + "/"
    records = [_record(source, reference, root, original_prefix) for reference in references]
    _write(root, original_path, raw)
    prepared = _derived_text(text, records, key).encode() if normalized else raw
    input_name = key + ".md" if normalized else source.name
    _write(root, "prepared/" + input_name, prepared)
    manifest = {
        "version": 1,
        "key": key,
        "source": attestation,
        "input_path": "prepared/" + input_name,
        "input_sha256": digest(prepared),
        "assets": records,
        "policy": POLICY,
    }
    _write(root, "bundle.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return manifest


def _verify_renderers(manifest: dict) -> None:
    if manifest["policy"] != POLICY:
        raise ValueError("bundle rendering policy drift")
    for row in manifest["assets"]:
        if row.get("renderer"):
            _, actual = _renderer(pathlib.Path(row["original_path"]).suffix.lower())
            if actual != row["renderer"]:
                raise ValueError("bundle renderer version/configuration drift")


def _verify_source(root: pathlib.Path, manifest: dict) -> None:
    from .sources import representation_for, source_attestation

    source = safe_path(root, "original/" + manifest["source"]["source_file"])
    if source_attestation(representation_for(source), root / "original") != manifest["source"]:
        raise ValueError("bundle original source attestation drift")
    normalized = manifest["source"]["adapter"] == "markdown"
    expected = _verify_mapping(source, manifest) if normalized else source.read_bytes()
    if digest(expected) != manifest["input_sha256"]:
        raise ValueError("bundle prepared mapping drift")


def _verify_mapping(source: pathlib.Path, manifest: dict) -> bytes:
    references, text = _visual_source(source.read_bytes().decode("utf-8"))
    actual = [{key: row[key] for key in reference} for row, reference in zip(manifest["assets"], references)]
    if actual != references or len(actual) != len(manifest["assets"]):
        raise ValueError("bundle reference mapping drift")
    return _derived_text(text, manifest["assets"], manifest["key"]).encode()


def verify_bundle(root: pathlib.Path, manifest: dict) -> None:
    """Reverify byte identity and configured renderer, without rerendering."""
    if (root.name, manifest["key"]) != (bundle_key(manifest["source"]),) * 2:
        raise ValueError("bundle source attestation identity drift")
    pairs = [
        ("original/" + manifest["source"]["source_file"], manifest["source"]["sha256"]),
        (manifest["input_path"], manifest["input_sha256"]),
    ]
    _verify_source(root, manifest)
    _verify_renderers(manifest)
    for row in manifest["assets"]:
        pairs.extend(
            (row[path], row[sha])
            for path, sha in (("original_path", "original_sha256"), ("derivative_path", "derivative_sha256"))
            if path in row
        )
    for relative, expected in pairs:
        if digest(_read(safe_path(root, relative))) != expected:
            raise ValueError(f"bundle asset drift: {relative}")


def publish_bundle(root: pathlib.Path, manifest: dict, wiki: pathlib.Path, doc_name: str) -> None:
    """Record native image outputs and preserve their originals for the exporter."""
    manifest_bytes = _read(root / "bundle.json")
    if json.loads(manifest_bytes) != manifest:
        raise ValueError("bundle manifest drift before publication")
    verify_bundle(root, manifest)
    original_prefix = f"assets/{manifest['key']}/"
    for relative, expected in publication_files(manifest, doc_name).items():
        if relative.startswith(original_prefix):
            source_relative = relative.removeprefix(original_prefix)
            _write(wiki, relative, safe_path(root, source_relative).read_bytes())
            continue
        if digest(_read(safe_path(wiki, relative))) != expected:
            raise ValueError(f"native asset drift: {relative}")
    _write(wiki, f"assets/{manifest['key']}/bundle.json", manifest_bytes)


def export_assets(wiki: pathlib.Path, docs: pathlib.Path) -> dict[str, str]:
    """Exporter consumes validated paths, never rediscovers source references."""
    files = {}
    for path in (wiki / "assets").glob("*/bundle.json"):
        manifest_bytes = _read(safe_path(wiki, path.relative_to(wiki).as_posix()))
        manifest = json.loads(manifest_bytes)
        if path.parent.name != manifest["key"]:
            raise ValueError("published bundle directory identity drift")
        doc_name = pathlib.Path(manifest["input_path"]).stem
        for relative, expected in publication_files(manifest, doc_name).items():
            data = _read(safe_path(wiki, relative))
            if digest(data) != expected:
                raise ValueError(f"published asset drift: {relative}")
            _write(docs, relative, data)
            files[relative] = expected
        _write(docs, path.relative_to(wiki).as_posix(), manifest_bytes)
    return files


def bundle_key(attestation: dict) -> str:
    return digest(json.dumps(attestation, sort_keys=True).encode())[:24]


def publication_files(manifest: dict, doc_name: str) -> dict[str, str]:
    if manifest["key"] != bundle_key(manifest["source"]):
        raise ValueError("published bundle source identity drift")
    source = manifest["source"]
    expected_name = manifest["key"] if source["adapter"] == "markdown" else pathlib.Path(source["source_file"]).stem
    if not doc_name == pathlib.Path(manifest["input_path"]).stem == expected_name:
        raise ValueError("native asset document identity mismatch")
    files = {}
    for row in manifest["assets"]:
        if "original_path" in row:
            files[f"assets/{manifest['key']}/{row['original_path']}"] = row["original_sha256"]
        if "derivative_path" in row:
            relative = f"sources/images/{doc_name}/{pathlib.Path(row['derivative_path']).name}"
            files[relative] = row["derivative_sha256"]
    return files
