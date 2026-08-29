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
    """Return (NOTE_BY_SOURCE, SOURCE_BY_FILE) from project + report book numbers.

    Single-witness (Odyssey): one file per book, canonical by construction.
    Multi-witness (Aeneid): the canonical (Latin) file per book is the
    authoritative source mapped to its note; witness files map to their own
    source ids but are NOT authoritative evidence (never in a claim's
    source_ids).
    """
    sid = project["source_id_template"]
    nt = project["note_template"]
    witnesses = list((project.get("witnesses") or {}).keys())
    if witnesses:
        # Note map is keyed by the CANONICAL source id only — claims'
        # source_ids are always the canonical (Latin) id for multi-witness
        # projects, so witness ids never need a note mapping.
        cid = project.get("canonical_source_id_template") or sid
        note_by_source = {cid.format(n=b): nt.format(n=b) for b in books}
    else:
        note_by_source = {sid.format(n=b): nt.format(n=b) for b in books}
    source_by_file = {}
    bt = project.get("book_label_template", "{n}.html")
    if witnesses:
        # multi-witness: label + source id are keyed by variant {w} and book {n}
        for b in books:
            for w in witnesses + [project.get("canonical_variant", "latin")]:
                try:
                    source_by_file[bt.format(n=b, w=w)] = sid.format(n=b, w=w)
                except KeyError:
                    # project's template doesn't carry {w}; fall back to plain {n}
                    source_by_file[bt.format(n=b)] = sid.format(n=b)
    else:
        for b in books:
            source_by_file[bt.format(n=b)] = sid.format(n=b)
    return note_by_source, source_by_file


def template_label(project: dict, b: int) -> str:
    """Resolve a snapshot's filename from the project's label template."""
    return project.get("book_label_template", "{n}.html").format(n=b)


# ---------------------------------------------------------------------------
# Multi-witness (canonical-latin-authoritative) schema validation.
#
# Backwards compatible: the base packet keeps `source_file`/`locator`/`excerpt`
# carrying the CANONICAL (Latin) evidence, so `source_ids` remains the
# authoritative evidence set exactly as before. The Aeneid worker ADDS an
# `english_witness` selection on top; it never replaces the Latin evidence.
# ---------------------------------------------------------------------------


def english_witness_defects(claim: dict, project: dict) -> list[str]:
    """Validate one claim's selected English witness (interpretive, not authority).

    The witness is the best English rendering of the cited Latin passage,
    chosen by the worker as *reviewable judgment* — the deterministic
    translation-grounding gate later proves the quoted English is verbatim in
    a valid witness that overlaps the aligned Latin passage.
    """
    out = []
    ew = claim.get("english_witness")
    if not isinstance(ew, dict):
        return ["english_witness is not an object"]
    wt = ew.get("translator")
    wsid = ew.get("source_id")
    wloc = ew.get("locator")
    wex = ew.get("excerpt")
    for label, val in (("translator", wt), ("source_id", wsid), ("locator", wloc), ("excerpt", wex)):
        if not isinstance(val, str) or not val.strip():
            out.append(f"english_witness missing/invalid {label}")
    if isinstance(wt, str) and wt:
        known = [str(v.get("translator")) for v in (project.get("witnesses") or {}).values() if isinstance(v, dict)]
        if known and wt not in known:
            out.append(f"english_witness translator {wt!r} not in project witnesses {known}")
    if isinstance(claim.get("witnesses_consulted"), str):
        out.append("witnesses_consulted must be a list")
    return out


def claim_type_defects(claim: dict, project: dict) -> list[str]:
    ct = claim.get("claim_type")
    allowed = set(project.get("claim_types") or [])
    if allowed and ct not in allowed:
        return [f"claim_type {ct!r} not in {sorted(allowed)}"]
    return []


def canonical_source_defects(claim: dict, project: dict) -> list[str]:
    """Requirement #8: the authoritative source_file must be the CANONICAL variant.

    A translation witness must never masquerade as authoritative evidence. In a
    multi-witness project the claim's ``source_file`` must be the
    canonical-variant snapshot (e.g. the Latin book file); if it names a
    witness-variant snapshot, that is a schema defect. (Which book a claim
    cites is a separate property, pinned by the excerpt-grounding gate: the
    excerpt must occur in the exact snapshot it cites.)
    """
    witnesses = project.get("witnesses") or {}
    if not witnesses:
        return []
    bt = project.get("book_label_template", "{n}.html")
    sf = str(claim.get("source_file", ""))
    m = re.search(r"book-(\d+)", sf)
    book = int(m.group(1)) if m else None
    if book is None:
        return []
    witness_labels = {bt.format(n=book, w=w) for w in witnesses}
    if sf in witness_labels:
        canon_label = bt.format(n=book, w=project.get("canonical_variant", "latin"))
        return [f"source_file {sf!r} is a translation witness, not the canonical {canon_label!r}"]
    return []


def multisource_packet_defects(parsed, project: dict, acceptance: dict | None = None):
    """Aeneid packet validation: base + claim_type + canonical-source + English witness.

    Runs the standard base checks first (so nothing is weakened), then the
    multi-witness field checks. Returns [] only when all pass.
    """
    defects = packet_defects(parsed, acceptance)
    if defects:
        return defects
    for idx, claim in enumerate(parsed.get("claims"), 1):
        defects.extend(f"claim {idx}: {d}" for d in claim_type_defects(claim, project))
        defects.extend(f"claim {idx}: {d}" for d in canonical_source_defects(claim, project))
        if (project.get("acceptance") or {}).get("latin_canonical_required"):
            defects.extend(f"claim {idx}: {d}" for d in english_witness_defects(claim, project))
    return defects
