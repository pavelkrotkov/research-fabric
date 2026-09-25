"""Candidate-bound deterministic review of compiled wiki outputs."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from urllib.parse import unquote, urlsplit

from ._reading_plan import published_reading_plan
from ._source_assets import published_assets
from .compilation import CompilationError, _git, write_json
from .run_state import _publication_outputs

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_LINK = re.compile(r"!?\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))")


def _scopes(kb):
    return [name for name in ("wiki", "evidence", "raw", ".gitattributes") if (kb / name).exists()]


def _changed(kb, scopes):
    changed = set(_git(kb, "diff", "--name-only", "HEAD", "--", *scopes).splitlines())
    changed.update(_git(kb, "ls-files", "--others", "--exclude-standard", "--", *scopes).splitlines())
    return sorted(filter(None, changed))


def _wikilink_candidate(wiki, raw):
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    logical = pathlib.PurePosixPath(target)
    if not target or logical.is_absolute() or ".." in logical.parts or "\\" in target:
        return None
    candidate = wiki.joinpath(*logical.parts)
    return candidate if candidate.suffix == ".md" else candidate.with_name(candidate.name + ".md")


def _wikilink_exists(wiki, raw):
    candidate = _wikilink_candidate(wiki, raw)
    if candidate is None:
        return False
    if candidate.is_file():
        return True
    relative = candidate.relative_to(wiki)
    return len(relative.parts) == 1 and any(path.is_file() for path in wiki.rglob(candidate.name))


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _canonical_target(target):
    parsed = urlsplit(target)
    return unquote(parsed.path) + (f"#{parsed.fragment}" if parsed.fragment else "")


def _citation_targets(kb):
    plan_path = kb / "evidence/reading-plan.json"
    if not plan_path.exists():
        return None, set()
    source_path = kb / "evidence/sources.jsonl"
    if not source_path.exists():
        raise ValueError("published reading plan has no source ledger")
    plan = published_reading_plan(kb, _jsonl(source_path))
    targets = {_canonical_target(plan.section(section_id)["target"]) for section_id in plan.data["sections"]}
    return targets, {target.split("#", 1)[0] for target in targets}


def _citation_defect(label, relative, fragment, citations, citation_paths):
    if citations is None:
        return None
    key = relative + (f"#{fragment}" if fragment else "")
    if relative in citation_paths:
        if key not in citations:
            return f"{label}: citation does not match frozen source section"
    elif "/original/" in relative and relative.endswith(".md"):
        return f"{label}: original-source citation is not in frozen reading plan"
    return None


def _local_link_defect(page, wiki, target, citations, citation_paths):
    label = page.relative_to(wiki)
    try:
        parsed = urlsplit(target)
    except ValueError:
        return f"{label}: malformed link {target}"
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    resolved = (page.parent / unquote(parsed.path)).resolve()
    try:
        relative = resolved.relative_to(wiki.resolve()).as_posix()
    except ValueError:
        return f"{label}: local link escapes wiki: {target}"
    if not resolved.is_file():
        return f"{label}: missing local target {target}"
    return _citation_defect(label, relative, parsed.fragment, citations, citation_paths)


def _page_defects(page, wiki, citations, citation_paths):
    text = page.read_text(encoding="utf-8")
    defects = [
        f"{page.relative_to(wiki)}: missing wikilink target [[{raw}]]"
        for raw in _WIKILINK.findall(text)
        if not _wikilink_exists(wiki, raw)
    ]
    for bracketed, plain in _LINK.findall(text):
        defect = _local_link_defect(page, wiki, bracketed or plain, citations, citation_paths)
        if defect:
            defects.append(defect)
    return defects


def _wiki_pages(wiki):
    """Semantic wiki pages only; native source views/policies have their own validators."""
    excluded = {"sources", "assets", "reports"}
    return (
        page
        for page in wiki.rglob("*.md")
        if page.is_file() and page.name != "AGENTS.md" and page.relative_to(wiki).parts[0] not in excluded
    )


def audit_compiled_wiki(kb, verification):
    """Snapshot the candidate and mechanically reject broken compiled-wiki references."""
    kb, verification = pathlib.Path(kb), pathlib.Path(verification)
    verification.mkdir(parents=True, exist_ok=True)
    outputs = _publication_outputs(kb)
    scopes = _scopes(kb)
    changed = _changed(kb, scopes)
    if scopes:
        _git(kb, "add", "-N", "-A", "--", *scopes)
    diff = _git(kb, "diff", "HEAD", "--no-ext-diff", "--", *scopes)
    (verification / "generated-diff.patch").write_text(diff, encoding="utf-8")
    defects = []
    try:
        citations, citation_paths = _citation_targets(kb)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        citations, citation_paths = None, set()
        defects.append(f"invalid published reading plan: {exc}")
    wiki = kb / "wiki"
    try:
        asset_hashes = published_assets(wiki)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        asset_hashes = {}
        defects.append(f"invalid published asset bundle: {exc}")
    for page in _wiki_pages(wiki):
        defects.extend(_page_defects(page, wiki, citations, citation_paths))
    if not diff.strip():
        defects.append("candidate diff is empty")
    base = _git(kb, "rev-parse", "HEAD")
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    binding = json.dumps({"base": base, "outputs": outputs, "diff_sha256": diff_sha}, sort_keys=True).encode()
    packet_path = kb / "evidence/packet-execution.json"
    packets = json.loads(packet_path.read_text()) if packet_path.exists() else {}
    report = {
        "version": 1,
        "base_commit": base,
        "candidate_sha256": hashlib.sha256(binding).hexdigest(),
        "outputs": outputs,
        "changed": changed,
        "diff_sha256": diff_sha,
        "diff_bytes": len(diff.encode()),
        "asset_manifest_hashes": asset_hashes,
        "derived_context": {
            worker: row["derived_context"] for worker, row in packets.items() if "derived_context" in row
        },
        "unit_coverage": "evidence/coverage.json" if (kb / "evidence/coverage.json").is_file() else None,
        "unit_outcomes": "evidence/unit-outcomes.json" if (kb / "evidence/unit-outcomes.json").is_file() else None,
        "mechanical": {"ok": not defects, "defects": defects},
        "derived_review": {worker: row["derived_review"] for worker, row in packets.items() if "derived_review" in row},
        "limitations": [
            "Byte/link/source-span checks do not establish semantic support or completeness.",
            "Semantic omission, nuance, duplication and preserved qualifications "
            "remain advisory human-review questions.",
        ],
    }
    write_json(verification / "compiled-wiki-review.json", report)
    if defects:
        raise CompilationError("Compiled wiki audit failed: " + "; ".join(defects[:5]))
    return report


def advisory_record(reply, scope, *, error=None):
    """Record an opinion without turning malformed/unavailable semantics into a gate."""
    text = (reply or "").strip()
    status = (
        "unavailable"
        if error or not text
        else "available"
        if "Scope:" in text and "Uncertainty:" in text
        else "malformed"
    )
    return {"status": status, "scope": scope, "response": text or None, "error": error}
