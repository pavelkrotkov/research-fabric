"""Opt-in raster inspection: derived notes, never original-source quotations.

A supplied raster is available, but not rendered by this module. It becomes
inspected only when the native get_image result is associated with its exact
bytes. Neither tool access nor generated transcription makes it reviewed.

Records retain an immutable request identity and a checksummed captured note.
Reusing that note avoids pretending a second model run is deterministic.
Cached records are advisory artifacts, not replacements for source manifests.
PDF/EPS rendering and validated source bundles belong to issue #12; section
packet consumption belongs to #11. This module handles only supplied rasters.
"""

import asyncio
import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path

from research_fabric._oauth_visual import ADAPTER_VERSION, query, supported_versions


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _config(spec):
    if set(spec) != {"model", "effort", "instructions", "assets"}:
        raise ValueError("visual spec requires only model, effort, instructions, assets")
    if not re.fullmatch(r"chatgpt/[^/]+", str(spec["model"])):
        raise ValueError("visual preflight requires an explicit chatgpt/ OAuth model; no API fallback")
    if spec["effort"] not in {"low", "medium", "high"}:
        raise ValueError("explicit supported effort and nonempty instructions required")
    if not spec["instructions"].strip():
        raise ValueError("nonempty instructions required")
    if not spec["assets"]:
        raise ValueError("at least one raster asset required")
    return {key: spec[key] for key in ("model", "effort", "instructions")}


def _assets(root, rows):
    from PIL import Image

    assets = []
    for row in rows:
        if set(row) != {"path", "sha256", "source_locator"} or not row["source_locator"]:
            raise ValueError("each raster requires path, sha256 and original source_locator")
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("only contained raster files supported; bundle rendering belongs to issue #12")
        if digest(path.read_bytes()) != row["sha256"]:
            raise ValueError("visual asset hash mismatch")
        with Image.open(path) as image:
            image.verify()
        assets.append(
            dict(
                row,
                tool_path=f"sources/images/{len(assets)}{path.suffix.lower()}",
                available=True,
                rendered=False,
                inspected=False,
                reviewed=False,
                derivative_sha256=row["sha256"],
            )
        )
    return assets


def _associate(call, assets):
    try:
        path = json.loads(call["arguments"])["image_path"]
        asset = assets[path]
    except (ValueError, TypeError, KeyError):
        return None
    if call["result"] != "image":
        return None
    call.update(asset_sha256=asset["sha256"], result_sha256=asset["derivative_sha256"])
    return path


def _inspection(record):
    assets = {a["tool_path"]: a for a in record["assets"]}
    successful = {_associate(call, assets) for call in record["calls"]}
    for path, asset in assets.items():
        asset["inspected"] = path in successful
    complete = successful == set(assets)
    if not complete or not record["note"].strip():
        record["unresolved"] = record["unresolved"] or "required visual access or final note incomplete"


def _query_record(source_root, config, record):
    # The agent receives only disposable, byte-verified raster copies. It never
    # receives canonical wiki paths or a write-capable native chat agent.
    with tempfile.TemporaryDirectory(prefix="research-visual-") as temporary:
        wiki = Path(temporary) / "wiki"
        for asset in record["assets"]:
            target = wiki / asset["tool_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (source_root / asset["path"]).read_bytes()
            if digest(data) != asset["sha256"]:
                raise ValueError("visual asset changed during preflight")
            target.write_bytes(data)
        config = dict(
            config,
            question="Inspect every listed raster with get_image, then transcribe or note uncertainty. "
            "This is derived evidence for human review, never an original-source quotation.\n"
            + json.dumps([{k: a[k] for k in ("tool_path", "source_locator")} for a in record["assets"]]),
        )
        try:
            record["note"] = asyncio.run(query(wiki, config, record["calls"]))
        except Exception as exc:
            # Provider exception strings can contain requests/tokens. No payloads
            # enter the record; retain the class and fail visual completion.
            record["unresolved"] = f"native query failed: {type(exc).__name__}"


def preflight(source_root, spec, output):
    """Run before preparation. Optional unresolved visuals remain explicit and advisory."""
    source_root, output = Path(source_root).resolve(), Path(output).resolve()
    config = _config(spec)
    versions = supported_versions()
    assets = _assets(source_root, spec["assets"])
    identity = {"config": config, "assets": assets, "versions": versions, "adapter": ADAPTER_VERSION}
    key = digest(json.dumps(identity, sort_keys=True).encode())
    if output.exists():
        record = json.loads(output.read_text())
        checksum = record.pop("record_sha256", None)
        if checksum != digest(json.dumps(record, sort_keys=True).encode()):
            raise ValueError("cached inspection record checksum mismatch")
        record["record_sha256"] = checksum
        if record["identity"] != key or record["request"] != identity:
            raise ValueError("existing visual note belongs to different inputs/config; use a new record path")
        return record
    record = {
        "request": identity,
        "identity": key,
        "assets": copy.deepcopy(assets),
        "kind": "derived_visual_note",
        "calls": [],
        "note": "",
        "unresolved": None,
    }
    _query_record(source_root, config, record)
    _inspection(record)
    record["record_sha256"] = digest(json.dumps(record, sort_keys=True).encode())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(output)
    return record


def preparation_note(record):
    return "DERIVED VISUAL NOTES — advisory, unreviewed, NOT verbatim source evidence:\n" + json.dumps(record)
