"""Deterministic, fail-closed EPUB source extraction."""

from __future__ import annotations

import hashlib
import io
import pathlib
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

_BLOCKS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "details", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_HEADINGS = {f"h{i}" for i in range(1, 7)}
_BREAKS = _BLOCKS | _HEADINGS
_SKIP = {"head", "script", "style"}
_MAX_MEMBER_SIZE = 64 * 1024 * 1024
_MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000
_MAX_XML_DEPTH = 128


def _xml(data: bytes, label: str) -> ET.Element:
    try:
        root = DET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise ValueError(f"DTD/entity declarations forbidden in EPUB XML: {label}") from exc
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
    invalid = any((parsed.scheme, parsed.netloc, parsed.query, not parsed.path, path.startswith(("../", "/")), path in (".", "..")))
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
        raise ValueErroЉ›X[›Ь›YYTP€\Ъ]™HЉHњ›ЫH^В€ћN‚€[™›ЬИH™‹љ[™›Ы\Э

B€ЬШY™WШ\Ъ]™J[™›ЬКB€[Y\ИHЪ[™›Л™љ[[[YH›Ь€[™›И[€[™›ЬЧB€Y€[Љ[Y\КHOH[ЉЩ]
[Y\КJN‚€Z\ЩH[YQ\њ›ЬЉ™\XШ]HTP€\Ъ]™HY[X™\€ЉB€Y€›ЭЭ[YЫZ[Y]\WЩ[ќћJ[™›ЬКN‚€Z\ЩH[YQ\њ›ЬЉљ[ќ[YTP€Z[Y]\H[ќћHЉB€Y€™‹њ™XY
›Z[Y]\HЉHOH€\XШ][Ы‹Щ\XЉЮљ\Ћ‚€Z\ЩH[YQ\њ›ЬЉљ[ќ[YTP€Z[Y]\HЉB€™]\›€™‹Щ]
[Y\КB€^Щ\^Щ\[ЫЋ‚€™‹ЫЬЩJ
B€Z\ЩB‚‚™Y€ЫX[љY™\Э
XЪШYЩN€U‘[[Y[ќXЪШYЩWЬ]€Э‹[Y\О€Щ]ЬЭ—JHO€XЭЬЭ‹\VЬЭ‹Э—WN‚€Э]]ИHЯKЩ]

B€›Ь€][H[€XЪШYЩK™љ[™[
‹‹ЛЮКџ[X[љY™\ЭЮКџZ][HЉN‚€][WЪY™Y‹YYXHH][K™Щ]
љYЉK][K™Щ]
љ™Y€ЉK][K™Щ]
›YYXK]\HЉB€Y€›Э[

][WЪY™Y‹YYXJJN‚€Z\ЩH[YQ\њ›ЬЉ›X[›Ь›YYTP€X[љY™\Э][HЉB€]HЫY[X™\ЉXЪШYЩWЬ]™YЉB€Y€][WЪY[€Э]Ь€][€]О‚€Z\ЩH[YQ\њ›Ь‹™\XШ]HTP€X[љY™\Э™\ЫЭ\ЩHЉB€Y€]›Э[€[Y\О‚€Z\ЩHљ[S›Э›Э[™\њ›ЬЉ€›Z\ЬЪ[™ИTP€™\ЫЭ\ЩN€Ь]HЉB€Э]Ъ][WЪYHH
]YYXJB€]ЛY
]
B€™]\›€Э]‚‚™Y€ЬЬ[™JXЪШYЩN€U‘[[Y[ќX[љY™\Э€XЭЬЭ‹\VЬЭ‹Э—WK™Ћ€љ\љ[K–љ\љ[JHO€\ЭЭ\VЬЭ‹ћ]\ЧWN‚€Ъ\\њЛЩY[€HЧKЩ]

B€›Ь€™Y€[€XЪШYЩK™љ[™[
‹‹ЛЮКџ\Ь[™KЮКџZ][\™Y€ЉN‚€][WЪYH™Y‹™Щ]
љY™Y€ЉB€Y€›Э][WЪYЬ€][WЪY[€ЩY[Ћ‚€Z\ЩH[YQ\њ›ЬЉ™\XШ]HЬ€Z\ЬЪ[™ИTP€Ь[™HY™Y€ЉB€ЩY[‹Y
][WЪY
B€Y€][WЪY›Э[€X[љY™\Э‚€Z\ЩHљ[S›Э›Э[™\њ›ЬЉ€›Z\ЬЪ[™ИTP€Ь[™H™\ЫЭ\ЩN€Ъ][WЪYHЉB€]YYXHHX[љY™\ЭЪ][WЪYB€Y€YYXHOH\XШ][Ы‹Ю[
Ю[Ћ‚€Z\ЩH[YQ\њ›ЬЉ€ќ[њЭ\ЬќYTP€Ь[™HYYXH\N€ЫYYX_HЉB€Ъ\\њЛ\[™

]™‹њ™XY
]
JJB€Y€›ЭЪ\\њО‚€Z\ЩH[YQ\њ›ЬЉ‘TP€Ь[™H\И[\HЉB€™]\›€Ъ\\њВ‚‚™Y€ЬXЪШYЩJ]О€ћ]\КHO€\VЫ\ЭЭ\VЬЭ‹ћ]\ЧWKЩ]ЬЭ—KXЭЬЭ‹Э—WN‚€™‹[Y\ИHШ\Ъ]™J]КB€ћN‚€Y€“QUKRS‘‹ШЫЫќZ[™\‹ћ[€›Э[€[Y\О‚€Z\ЩHљ[S›Э›Э[™\њ›ЬЉ›Z\ЬЪ[™ИTP€™\ЫЭ\ЩN€QUKRS‘‹ШЫЫќZ[™\‹ћ[ЉB€ЫЫќZ[™\€HЮ[
™‹њ™XY
“QUKRS‘‹ШЫЫќZ[™\‹ћ[ЉKЫЫќZ[™\‹ћ[ЉB€›ЫЭИHЫ›ЩK™Щ]
™ќ[\]ЉH›Ь€›ЩH[€ЫЫќZ[™\‹™љ[™[
‹‹ЛЮКџ\›ЫЭљ[HЉHY€›ЩK™Щ]
™ќ[\]ЉWB€Y€[Љ›ЫЭКHOHN‚€Z\ЩH[YQ\њ›ЬЉ‘TP€]\ЭЫЫќZ[€^XЭHЫ™HXЪШYЩH›ЫЭљ[HЉB€XЪШYЩWЬ]HЫY[X™\Љ€‹›ЫЭЦМJB€Y€XЪШYЩWЬ]›Э[€[Y\О‚€Z\ЩHљ[S›Э›Э[™\њ›ЬЉ€›Z\ЬЪ[™ИTP€XЪШYЩN€ЬXЪШYЩWЬ]HЉB€XЪШYЩHHЮ[
™‹њ™XY
XЪШYЩWЬ]
KXЪШYЩWЬ]
B€X[љY™\ЭHЫX[љY™\Э
XЪШYЩKXЪШYЩWЬ][Y\КB€Ъ\\њИHЬЬ[™JXЪШYЩKX[љY™\Э™ЉB€™]\›€Ъ\\њЛЬ]›Ь€]И[€X[љY™\Эќ[Y\К
_KЫY]Y]JXЪШYЩKXЪШYЩWЬ][ЉЪ\\њКJB€љ[[N‚€™‹ЫЬЩJ
B‚‚™Y€ЫY]Y]WЭ[YJY]Y]N€U‘[[Y[ќЩ^N€ЭЉHO€ЭЋ‚€[Y\ИH
€‹љ›Ъ[Љ›ЩKљ]\ќ^

JKњЭљ\

H›Ь€›ЩH[€Y]Y]K™љ[™[
€ћЮКџ_^ЪЩ^_HЉJB€™]\›€€‹љ›Ъ[Љљ[\Љ›Ы™K[Y\КJB‚‚™Y€ЫY]Y]JXЪШYЩN€U‘[[Y[ќXЪШYЩWЬ]€Э‹ЫЭ[ќ€[ќ
HO€XЭЬЭ‹Э—N‚€Э]HИњXЪШYЩWЬ]Ћ€XЪШYЩWЬ]њЬ[™WЪ][\ИЋ€ЭЉЫЭ[ќ
_B€Y€™\њЪ[Ы€ЏHXЪШYЩK™Щ]
ќ™\њЪ[Ы€ЉN‚€Э]Иќ™\њЪ[Ы€—HH™\њЪ[Ы‚€Y]Y]HHXЪШYЩK™љ[™
‹‹ЛЮКџ[Y]Y]HЉB€Y€Y]Y]H\И›Ы™N‚€™]\›€Э]€›Ь€Щ^H[€
ќ]H‹›[™ЭXYЩH‹љY[ќYљY\€ЉN‚€Y€[YHЏHЫY]Y]WЭ[YJY]Y]KЩ^JN‚€Э]ЪЩ^WHH[YB€™]\›€Э]‚‚™Y€ЭYК›ЩN€U‘[[Y[ќ
HO€ЭЋ‚€™]\›€›ЩKќYЛњњЬ]
џH‹JVЛLWK›ЭЩ\Љ
B‚‚™Y€Ш\ЬЩ]
›ЩN€U‘[[Y[ќЪ\\Ћ€Э‹™\ЫЭ\Щ\О€Щ]ЬЭ—JHO€ЭЋ‚€\™Щ]H
€›ЩK™Щ]
њЬИЉB€Ь€›ЩK™Щ]
љ™Y€ЉB€Ь€™^

[YH›Ь€Щ^K[YH[€›ЩK]љX‹љ][\К
HY€Щ^K™[™ЭЪ]
џZ™Y€ЉJK›Ы™JB€
B€Y€›Э\™Щ]‚€™]\›€€‚€]HЫY[X™\ЉЪ\\‹\™Щ]
B€Y€]›Э[€™\ЫЭ\Щ\О‚€Z\ЩHљ[S›Э›Э[™\њ›ЬЉ€›Z\ЬЪ[™ИTP€\ЬЩ]€Ь]HЉB€™]\›€€—ђ\ЬЩ]Ь]W€‚‚‚™Y€ЪXY[™ЧЫШШ]ЬЉ›ЩN€U‘[[Y[ќXY[™ЬО€\ЭЪ[ќKYО€Щ]ЬЭ—JHO€ЭЋ‚€XY[™ЬЦМH
ПHB€Y€ШШ]Ь€ЏH›ЩK™Щ]
љYЉN‚€™]\›€ШШ]Ь‚€ШШ]Ь€H€љЪXY[™ЬЦМ_H‚€Ъ[HШШ]Ь€[€YО‚€ШШ]Ь€H€—ЮЫШШ]ЬџH‚€YЛY
ШШ]ЬЉB€™]\›€ШШ]Ь‚‚‚™Y€Ь™Yљ^
›ЩN€U‘[[Y[ќЪ\\Ћ€Э‹™\ЫЭ\Щ\О€Щ]ЬЭ—KXY[™ЬО€\ЭЪ[ќKYО€Щ]ЬЭ—JHO€ЭЋ‚€YИHЭYК›ЩJB€Y€YИ[€
љ[YИ‹љ[XYЩHЉN‚€™]\›€Ш\ЬЩ]
›ЩKЪ\\‹™\ЫЭ\Щ\КB€Y€YИ[€ТPQS‘ФО‚€ШШ]Ь€HЪXY[™ЧЫШШ]ЬЉ›ЩKXY[™ЬЛYКB€™]\›€€—ђЩXЭ[Ы€ШЪ\\џHЮЫШШ]ЬџWћЙИЙИ
€[ќ
YЦМWJ_H‚€Y€YИ[€Р“РТФО‚€™]\›€—€‚€™]\›€€›Ь›][H€Y€YИOH›X]€[ЩH€‚‚‚™Y€ЬЭ™ЧШ\ЬЩ]К›ЩN€U‘[[Y[ќЪ\\Ћ€Э‹™\ЫЭ\Щ\О€Щ]ЬЭ—JHO€ЭЋ‚€™]\›€€‹љ›Ъ[ЉШ\ЬЩ]
Ъ[Ъ\\‹™\ЫЭ\Щ\КH›Ь€Ъ[[€›ЩKљ]\Љ
HY€ЭYКЪ[
HOHљ[XYЩHЉB‚‚™Y€Ь™[™\Љ›ЩN€U‘[[Y[ќЪ\\Ћ€Э‹™\ЫЭ\Щ\О€Щ]ЬЭ—KXY[™ЬО€\ЭЪ[ќKYО€Щ]ЬЭ—JHO€ЭЋ‚€YИHЭYК›ЩJB€Y€YИOHњЭ™ИЋ‚€™]\›€ЬЭ™ЧШ\ЬЩ]К›ЩKЪ\\‹™\ЫЭ\Щ\КB€Y€YИ[€ФТТT‚€™]\›€€‚€Э]HЧЬ™Yљ^
›ЩKЪ\\‹™\ЫЭ\Щ\ЛXY[™ЬЛYКK›ЩKќ^Ь€€—B€›Ь€Ъ[[€›ЩN‚€Э]™^[™

Ь™[™\ЉЪ[Ъ\\‹™\ЫЭ\Щ\ЛXY[™ЬЛYКKЪ[ќZ[Ь€€ЉJB€Y€YИ[€Р”‘PRФО‚€Э]\[™
—€ЉB€™]\›€€‹љ›Ъ[ЉЭ]
B‚‚™Y€ШЪ\\Љ]€Э‹]N€ћ]\Л™\ЫЭ\Щ\О€Щ]ЬЭ—JHO€ЭЋ‚€›ЫЭHЮ[
]K]
B€YИHЭ[YH›Ь€›ЩH[€›ЫЭљ]\Љ
HY€
[YHЏH›ЩK™Щ]
љYЉJ_B€^HЬ™[™\Љ›ЫЭ]™\ЫЭ\Щ\ЛМKYКB€^H™KњЭXЉ€–ИJИ‹€‹^
B€^H™KњЭXЉ€€
—€
€‹—€‹^
B€™]\›€™KњЭXЉ€—ћМЛH‹——€‹^
KњЭљ\

B‚‚™Y€Э^
]О€ћ]\КHO€ЭЋ‚€Ъ\\њЛ™\ЫЭ\Щ\ЛИHЬXЪШYЩJ]КB€Э]ЩY[€HЧKЩ]

B€›Ь€]]H[€Ъ\\њО‚€YЩ\ЭH\ЪX‹њЪLЌMЉ]JK™YЩ\Э

B€Y€YЩ\Э[€ЩY[Ћ‚€Z\ЩH[YQ\њ›ЬЉ€™\XШ]HTP€Ь[™HЫЫќ[ќ€Ь]HЉB€ЩY[‹Y
YЩ\Э
B€Э]\[™
€ђЪ\\€Ь]WћЧШЪ\\Љ]]K™\ЫЭ\Щ\К_HЉB€™]\›€——€‹љ›Ъ[ЉЭ]
B‚‚Ы\ЬИTPђY\\Ћ‚€[YHH™\X€‚€™\њЪ[Ы€HЊH‚€ЭY™љ^\ИH
‹™\X€‹
B‚€Y€XЫЩJЩ[‹]О€ћ]\КHO€ћ]\О‚€™]\›€]В‚€Y€^XЭЭ^
Щ[‹XЫЩY€ћ]\КHO€ЭЋ‚€™]\›€Э^
XЫЩY
B‚€Y€X\ЫШШ]ЬЉЩ[‹ШШ]ЬЋ€ЭЉHO€ЭЋ‚€™]\›€ШШ]Ь‹њЭљ\

B‚€Y€\ЬЩ]КЩ[‹XЫЩY€ћ]\КHO€\VЬЭ‹‹‹—N‚€™]\›€

B‚€Y€Y]Y]JЩ[‹]€]X‹”]
HO€XЭЬЭ‹Э—N‚€ЛЛXЪШYЩHHЬXЪШYЩJ]њ™XYШћ]\К
JB€™]\›€ИЫЫќ[ќЭ\HЋ€\XШ][Ы‹Щ\XЉЮљ\‹™љ[[[YHЋ€]›[YK
ЉњXЪШYЩ_B