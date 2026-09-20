"""Render original-source citations from a validated frozen reading plan."""

from __future__ import annotations

import json
import pathlib
from urllib.parse import quote

from ._reading_span import span_range, span_text


def section(plan, section_id):
    """Return the original-source target and immutable byte range for one section."""
    span = plan.data["sections"][section_id]
    rep = plan.representations[span["source_file"]]
    start, end = span_range(rep, span)
    key = plan.data["sources"][span["source_file"]]["bundle_key"]
    source_file = span["source_file"]
    logical = pathlib.PurePosixPath(source_file)
    if logical.is_absolute() or ".." in logical.parts or "\\" in source_file:
        raise ValueError(f"unsafe reading source path: {source_file}")
    source = quote(source_file, safe="/")
    return {
        **span,
        "target": f"assets/{key}/original/{source}#L{span['lines'][0]}-L{span['lines'][1] - 1}",
        "original_bytes": [rep.original_byte_offset(start), rep.original_byte_offset(end)],
    }


def citation_policy(plan):
    """Map native OpenKB citations to the same frozen original-source sections."""
    records = []
    for section_id in plan.data["sections"]:
        record = section(plan, section_id)
        heading = span_text(plan.representations, record).splitlines()[0]
        records.append(json.dumps({"section": section_id, "heading": heading, **record}, ensure_ascii=False))
    return (
        "\n## Frozen research source citations\n"
        f"Reading plan SHA256: {plan.data['sha256']}\n"
        "Cite original source sections using the targets below (paths relative to wiki root; "
        "from summaries/concepts use ../assets/...). Prepared document line numbers are not original lines. "
        "Retain mathematical qualifications, definitions, uncertainty and conflicting results. "
        "Context is not independent primary evidence. Bibliographic unknowns remain unknown. "
        "These mappings identify original ranges; they do not prove semantic support.\n" + "\n".join(records) + "\n"
    )
