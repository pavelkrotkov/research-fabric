"""Pure, dependency-free core logic for the research-fabric engine.

Contains the deterministic, CAO-free pieces that are unit-testable on any
runner (including a public CI without the controller box, CAO, OpenKB, or a
model key): packet validation, normalization, JSON extraction, project-spec
loading, and label/source-id templating.

Everything here is deterministic given its inputs. Modules that need CAO
(run steps) or the live host (OpenKB, direct worker) import from here and add
their orchestration on top.
"""

from __future__ import annotations

import json
import pathlib
import re

# Field keys every project spec must carry for the engine to drive a corpus.
REQUIRED_PROJECT_FIELDS = (
    "corpus_dir",
    "manifest_path",
    "snapshot_pattern",
    "source_id_template",
    "note_template",
)

# Stance values the schema contract accepts.
VALID_STANCES = {"supports", "qualifies", "contradicts"}

# Literal strings that mark a placeholder/schema-echo value (not real evidence).
PLACEHOLDER_STRINGS = ("...", "\u2026", "TBD")


def load_project(projects_dir: pathlib.Path, name: str) -> dict:
    """Load and validate a project spec, failing fast on missing required keys."""
    import yaml

    path = projects_dir / f"{name}.yaml"
    if not path.exists():
        raise RuntimeError(f"no project spec at {path}")
    proj = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [f for f in REQUIRED_PROJECT_FIELDS if f not in proj]
    if missing:
        raise RuntimeError(f"project '{name}' spec missing required fields: {', '.join(missing)}")
    return proj


def extract_json(text: str):
    """Parse a model/verifier reply into a JSON object, tolerating fences and
    surrounding prose. Prefers the protocol's distinctive ``{\"claims\"`` opener so
    a search locator cannot win."""
    text = (text or "").strip()
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    candidates.extend(fenced)
    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except Exception:
            pass
    decoder = json.JSONDecoder()
    preferred = text.find('{"claims"')
    starts = ([preferred] if preferred >= 0 else []) + [
        start for start, char in enumerate(text) if char in "[{" and start != preferred
    ]
    for start in starts:
        try:
            candidate = text[start:]
            # Terminal capture hard-wraps long JSON lines; inside JSON strings
            # those visual wraps are invalid, so drop indentation on the
            # protocol candidate.
            candidate = re.sub(r"\n[ \t]+", " ", candidate)
            value, _ = decoder.raw_decode(candidate)
            if isinstance(value, dict) and isinstance(value.get("claims"), list):
                return value
        except Exception:
            continue
    return None


def normalize_packet(value):
    """Undo terminal visual wrapping without weakening evidence checks."""
    for claim in value.get("claims", []):
        if isinstance(claim, dict):
            if isinstance(claim.get("source_file"), str):
                claim["source_file"] = re.sub(r"\s+", "", claim["source_file"])
            if isinstance(claim.get("excerpt"), str):
                claim["excerpt"] = re.sub(r"\s+", " ", claim["excerpt"]).strip()
    for conflict in value.get("conflicts", []):
        if isinstance(conflict, dict):
            if isinstance(conflict.get("source_file"), str):
                conflict["source_file"] = re.sub(r"\s+", "", conflict["source_file"])
            if isinstance(conflict.get("source_files"), list):
                conflict["source_files"] = [
                    re.sub(r"\s+", "", path) if isinstance(path, str) else path for path in conflict["source_files"]
                ]
            if isinstance(conflict.get("locator"), str):
                conflict["locator"] = re.sub(r"\s+", " ", conflict["locator"]).strip()
    return value


def packet_defects(parsed, acceptance: dict | None = None):
    """Return reasons a packet is unusable as evidence, or [] when acceptable.

    A syntactically valid packet can still be evidentially empty (a worker that
    never read its source returns ``{"claims": []}``). Reject empty packets,
    placeholder schema echoes, non-concrete stances, and violation of declared
    per-book claim bounds.
    """
    if not isinstance(parsed, dict):
        return ["packet is not a JSON object"]
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return ["packet has no claims list"]
    if not claims:
        notes = parsed.get("coverage_notes") or []
        detail = f"; coverage_notes={notes[:2]}" if notes else ""
        return [f"packet contains zero claims{detail}"]

    acceptance = acceptance or {}
    lo = acceptance.get("min_claims_per_book")
    hi = acceptance.get("max_claims_per_book")
    if lo is not None and len(claims) < lo:
        return [f"packet has {len(claims)} claims; below min_claims_per_book={lo}"]
    if hi is not None and len(claims) > hi:
        return [f"packet has {len(claims)} claims; above max_claims_per_book={hi}"]

    defects = []
    for idx, claim in enumerate(claims, 1):
        if not isinstance(claim, dict):
            defects.append(f"claim {idx} is not an object")
            continue
        for field in ("claim", "source_file", "locator", "excerpt"):
            value = claim.get(field)
            if not isinstance(value, str) or not value.strip():
                defects.append(f"claim {idx} missing {field}")
        stance = claim.get("stance", "")
        if stance not in VALID_STANCES:
            defects.append(f"claim {idx} has non-concrete stance: {stance!r}")
        if all(
            claim.get(f) in PLACEHOLDER_STRINGS or claim.get(f) is None
            for f in ("claim", "source_file", "locator", "excerpt")
        ):
            defects.append(f"claim {idx} is a placeholder schema echo")
    return defects


def book_task_from_project(b: int, project: dict, project_name: str) -> str:
    """Claim-extraction brief for one book, themed to its narrative arc."""
    themes = {int(k): v for k, v in (project.get("themes") or {}).items()}
    t = themes.get(
        b,
        project.get("default_theme", "its central events and their moral and theological significance"),
    )
    return (
        f"Analyze only Book {b}. Extract claim-level evidence about {t}. "
        f"Use the {project_name} snapshot for Book {b} only."
    )


def source_mappings(project: dict, books):
    """Return (NOTE_BY_SOURCE, SOURCE_BY_FILE) from project + report book numbers."""
    sid = project["source_id_template"]
    nt = project["note_template"]
    bt = project.get("book_label_template", "{n}.html")
    note_by_source = {sid.format(n=b): nt.format(n=b) for b in books}
    source_by_file = {bt.format(n=b): sid.format(n=b) for b in books}
    return note_by_source, source_by_file


def template_label(project: dict, b: int) -> str:
    """Resolve a snapshot's filename from the project's label template."""
    return project.get("book_label_template", "{n}.html").format(n=b)
