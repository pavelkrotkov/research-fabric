"""Candidate-bound deterministic review of compiled wiki outputs."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
from urllib.parse import unquote, urlsplit

from ._reading_plan import published_reading_plan
from .compilation import CompilationError, write_json
from .run_state import _publication_outputs

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_LINK = re.compile(r"!?\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))")


def _git(kb, *args):
    return subprocess.run(
        ["git", "-C", str(kb), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def _scopes(kb):
    return [name for name in ("wiki", "evidence", "raw", ".gitattributes") if (kb / name).exists()]


def _changed(kb, scopes):
    changed = set(_git(kb, "diff", "--name-only", "HEAD", "--", *scopes).splitlines())
    changed.update(_git(kb, "ls-files", "--others", "--exclude-standard", "--", *scopes).splitlines())
    return sorted(filter(None, changed))


def _wikilink_exists(wiki, raw):
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    logical = pathlib.PurePosixPath(target)
    if not target or logical.is_absolute() or ".." in logical.parts or "\\" in target:
        return False
    candidate = wiki.joinpath(*logical.parts)
    if not candidate.suffix:
        candidate = candidate.with_suffix(".md")
    return candidate.is_file() or any(wiki.rglob(pathlib.Path(target).with_suffix(".md").name))


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


def _page_defects(page, wiki, citations, citation_paths):
    text = page.read_text(encoding="utf-8")
    defects = [
        f"{page.relative_to(wiki)}: missing wikilink target [[{raw}]]"
        for raw in _WIKILINK.findall(text)
        if not _wikilink_exists(wiki, raw)
    ]
    for bracketed, plain in _LINK.findall(text):
        target = bracketed or plain
        try:
            parsed = urlsplit(target)
        except ValueError:
            defects.append(f"{page.relative_to(wiki)}: malformed link {target}")
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        resolved = (page.parent / unquote(parsed.path)).resolve()
        try:
            relative = resolved.relative_to(wiki.resolve()).as_posix()
        except ValueError:
            defects.append(f"{page.relative_to(wiki)}: local link escapes wiki: {target}")
            continue
        if not resolved.is_file():
            defects.append(f"{page.relative_to(wiki)}: missing local target {target}")
            continue
        if citations is None:
            continue
        key = relative + (f"#{parsed.fragment}" if parsed.fragment else "")
        if relative in citation_paths and key not in citations:
            defects.append(f"{page.relative_to(wiki)}: citation does not match frozen source section: {target}")
        elif "/original/" in relative and relative.endswith(".md") and relative not in citation_paths:
            defects.append(f"{page.relative_to(wiki)}: original-source citation is not in frozen reading plan: {target}")
    return defects


def audit_compiled_wiki(kb, verification):
    """Snapshot the candidate and mechanically reject broken compiled-wiki references."""
    kb, verification = pathlib.Path(kb), pathlib.Path(verification)
    verification.mkdir(parents=True, exist_ok=True)
    outputs = _publication_outputs(kb)
    scopes = _scopes(kb)
    changed = _changed(kb, scopes)
    if scopes:
        subprocess.run(["git", "-C", str(kb), "add", "-N", "-A", "--", *scopes], check=True)
    diff = _git(kb, "diff", "--no-ext-diff", "--", *scopes)
    (verification / "generated-diff.patch").write_text(diff, encoding="utf-8")
    defects = []
    try:
        citations, citation_paths = _citation_targets(kb)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        citations, citation_paths = None, set()
        defects.append(f"invalid published reading plan: {exc}")
    wiki = kb / "wiki"
    for relative in changed:
        page = kb / relative
        if relative.startswith("wiki/") and page.suffix == ".md" and page.is_file():
            defects.extend(_page_defects(page, wiki, citations, citation_paths))
    if not diff.strip():
        defects.append("candidate diff is empty")
    base = _git(kb, "rev-parse", "HEAD")
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    binding = json.dumps({"base": base, "outputs": outputs, "diff_sha256": diff_sha}, sort_keys=True).encode()
    report = {
        "version": 1,
        "base_commit": base,
        "candidate_sha256": hashlib.sha256(binding).hexdigest(),
        "outputs": outputs,
        "changed": changed,
        "diff_sha256": diff_sha,
        "diff_bytes": len(diff.encode()),
        "mechanical": {"ok": not defects, "defects": defects},
        "limitations": [
            "Byte/link/source-span checks do not establish semantic support or completeness.",
            "Semantic omission, nuance, duplication and preserved qualifications remain advisory human-review questions.",
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
