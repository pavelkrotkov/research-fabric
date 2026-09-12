"""Deterministic EPUB source extraction using only the standard library."""

from __future__ import annotations

import hashlib
import io
import pathlib
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

_BLOCKS = {"p", "div", "li", "blockquote", "pre", "tr", "td", "th", "section", "article", "br"}
_HEADINGS = {f"h{i}" for i in range(1, 7)}
_SKIP = {"head", "script", "style", "svg"}
_MAX_MEMBER_SIZE = 64 * 1024 * 1024
_MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000
_MAX_XML_DEPTH = 128
_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _xml(data: bytes, label: str) -> ET.Element:
    if _FORBIDDEN_XML.search(data):
        raise ValueError(f"DTD/entity declarations forbidden in EPUB XML: {label}")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed EPUB XML {label}: {exc}") from exc
    _check_depth(root, label)
    return root


def _check_depth(root: ET.Element, label: str) -> None:
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_XML_DEPTH:
            raise ValueError(f"EPUB XML nesting exceeds {_MAX_XML_DEPTH}: {label}")
        stack.extend((child, depth + 1) for child in node)


def _member(base: str, href: str) -> str:
    parsed = urlsplit(href)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    invalid = any((parsed.scheme, parsed.netloc, not parsed.path, path.startswith(("../", "/")), path in (".", "..")))
    if invalid:
        raise ValueError(f"unsafe EPUB resource: {href}")
    return path


def _valid_mimetype_entry(infos: list[zipfile.ZipInfo]) -> bool:
    if not infos:
        return False
    first = infos[0]
    return first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED


def _safe_member(info: zipfile.ZipInfo) -> None:
    if info.file_size > _MAX_MEMBER_SIZE:
        raise ValueError(f"EPUB member too large: {info.filename}")
    if info.file_size and not info.compress_size:
        raise ValueError(f"unsafe EPUB compression ratio: {info.filename}")
    if info.compress_size and info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO:
        raise ValueError(f"unsafe EPUB compression ratio: {info.filename}")


def _safe_archive(infos: list[zipfile.ZipInfo]) -> None:
    total = 0
    for info in infos:
        _safe_member(info)
        total += info.file_size
        if total > _MAX_ARCHIVE_SIZE:
            raise ValueError("EPUB uncompressed size limit exceeded")


def _archive(raw: bytes) -> tuple[zipfile.ZipFile, set[str]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("malformed EPUB archive") from exc
    try:
        infos = zf.infolist()
        _safe_archive(infos)
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("duplicate EPUB archive member")
        if not _valid_mimetype_entry(infos):
            raise ValueError("invalid EPUB mimetype entry")
        if zf.read("mimetype") != b"application/epub+zip":
            raise ValueError("invalid EPUB mimetype")
        return zf, set(names)
    except Exception:
        zf.close()
        raise


def _manifest(package: ET.Element, package_path: str, names: set[str]) -> dict[str, tuple[str, str]]:
    out, paths = {}, set()
    for item in package.findall(".//{*}manifest/{*}item"):
        item_id, href, media = item.get("id"), item.get("href"), item.get("media-type")
        if not all((item_id, href, media)):
            raise ValueError("malformed EPUB manifest item")
        path = _member(package_path, href)
        if item_id in out or path in paths:
            raise ValueError("duplicate EPUB manifest resource")
        if path not in names:
            raise FileNotFoundError(f"missing EPUB resource: {path}")
        out[item_id] = (path, media)
        paths.add(path)
    return out


def _spine(package: ET.Element, manifest: dict[str, tuple[str, str]], zf: zipfile.ZipFile) -> list[tuple[str, bytes]]:
    chapters, seen = [], set()
    for ref in package.findall(".//{*}spine/{*}itemref"):
        item_id = ref.get("idref")
        if not item_id or item_id in seen:
            raise ValueError("duplicate or missing EPUB spine idref")
        seen.add(item_id)
        if item_id not in manifest:
            raise FileNotFoundError(f"missing EPUB spine resource: {item_id}")
        path, media = manifest[item_id]
        if media != "application/xhtml+xml":
            raise ValueError(f"unsupported EPUB spine media type: {media}")
        chapters.append((path, zf.read(path)))
    if not chapters:
        raise ValueError("EPUB spine is empty")
    return chapters


def _package(raw: bytes) -> tuple[list[tuple[str, bytes]], set[str], dict[str, str]]:
    zf, names = _archive(raw)
    try:
        if "META-INF/container.xml" not in names:
            raise FileNotFoundError("missing EPUB resource: META-INF/container.xml")
        container = _xml(zf.read("META-INF/container.xml"), "container.xml")
        roots = [node.get("full-path") for node in container.findall(".//{*}rootfile") if node.get("full-path")]
        if len(roots) != 1:
            raise ValueError("EPUB must contain exactly one package rootfile")
        package_path = _member("", roots[0])
        if package_path not in names:
            raise FileNotFoundError(f"missing EPUB package: {package_path}")
        package = _xml(zf.read(package_path), package_path)
        manifest = _manifest(package, package_path, names)
        chapters = _spine(package, manifest, zf)
        return chapters, {path for path, _ in manifest.values()}, _metadata(package, package_path, len(chapters))
    finally:
        zf.close()


def _metadata_value(metadata: ET.Element, key: str) -> str:
    values = (" ".join(node.itertext()).strip() for node in metadata.findall(f"{{*}}{key}"))
    return " | ".join(filter(None, values))


def _metadata(package: ET.Element, package_path: str, count: int) -> dict[str, str]:
    out = {"package_path": package_path, "spine_items": str(count)}
    if version := package.get("version"):
        out["version"] = version
    metadata = package.find(".//{*}metadata")
    if metadata is None:
        return out
    for key in ("title", "language", "identifier"):
        if value := _metadata_value(metadata, key):
            out[key] = value
    return out


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()


def _asset(node: ET.Element, chapter: str, resources: set[str]) -> str:
    target = (
        node.get("src")
        or node.get("href")
        or next((value for key, value in node.attrib.items() if key.endswith("}href")), None)
    )
    if not target:
        return ""
    path = _member(chapter, target)
    if path not in resources:
        raise FileNotFoundError(f"missing EPUB asset: {path}")
    return f"\n@@asset {path}\n"


def _prefix(node: ET.Element, chapter: str, resources: set[str], headings: list[int]) -> str:
    tag = _tag(node)
    if tag in ("img", "image"):
        return _asset(node, chapter, resources)
    if tag in _HEADINGS:
        headings[0] += 1
        locator = node.get("id") or f"h{headings[0]}"
        return f"\n@@section {chapter}#{locator}\n{'#' * int(tag[1])} "
    if tag in _BLOCKS:
        return "\n"
    return " @@formula " if tag == "math" else ""


def _render(node: ET.Element, chapter: str, resources: set[str], headings: list[int]) -> str:
    tag = _tag(node)
    if tag in _SKIP:
        return ""
    out = [_prefix(node, chapter, resources, headings), node.text or ""]
    for child in node:
        out.extend((_render(child, chapter, resources, headings), child.tail or ""))
    if tag in _BLOCKS or tag in _HEADINGS:
        out.append("\n")
    return "".join(out)


def _chapter(path: str, data: bytes, resources: set[str]) -> str:
    root = _xml(data, path)
    text = _render(root, path, resources, [0])
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text(raw: bytes) -> str:
    chapters, resources, _ = _package(raw)
    out, seen = [], set()
    for path, data in chapters:
        digest = hashlib.sha256(data).digest()
        if digest in seen:
            raise ValueError(f"duplicate EPUB spine content: {path}")
        seen.add(digest)
        out.append(f"@@chapter {path}\n{_chapter(path, data, resources)}")
    return "\n\n".join(out)


class EPUBAdapter:
    name = "epub"
    version = "1"
    suffixes = (".epub",)

    def decode(self, raw: bytes) -> bytes:
        _package(raw)
        return raw

    def extract_text(self, decoded: bytes) -> str:
        return _text(decoded)

    def map_locator(self, locator: str) -> str:
        return locator.strip()

    def assets(self, decoded: bytes) -> tuple[str, ...]:
        return ()

    def metadata(self, path: pathlib.Path) -> dict[str, str]:
        _, _, package = _package(path.read_bytes())
        return {"content_type": "application/epub+zip", "filename": path.name, **package}
