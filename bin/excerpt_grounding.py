#!/usr/bin/env python3
"""Deterministic excerpt-grounding gate for the research evidence ledger.

Every claim asserts an ``excerpt`` copied from a source snapshot. This module
proves that assertion mechanically instead of delegating it to an agent's
judgement.

Why normalization is required
-----------------------------
The evidence workers read HTML snapshots through a terminal UI. Three faithful
transformations happen along that path and none of them change a single content
word:

1. HTML entities are rendered (``&quot;`` -> ``"``).
2. Typographic punctuation is folded (``—``/``’``/``”`` vs ``-``/``'``/``"``).
3. Whitespace is reflowed, including spaces introduced around dashes.

Normalization tolerates exactly those three classes and nothing else. Content
words, their order, numbers, and negations must match the source byte-for-byte
after folding. A claim whose words are not in the source still fails.

The accompanying self-test includes negative controls (deleted negation,
changed number, reordered clause, invented sentence) which MUST be rejected;
the gate refuses to run if any control passes.
"""
from __future__ import annotations
import html, json, pathlib, re, sys, unicodedata

# Punctuation folding table: typographic variants -> ASCII equivalents.
_PUNCT = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ",
    "\u2026": "...",
}


def fold(text: str) -> str:
    """Normalize a string for transcription-tolerant comparison."""
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(_PUNCT.get(ch, ch) for ch in text)
    # Collapse all whitespace, then remove whitespace adjacent to dashes so
    # "hands -fishing", "hands- fishing" and "hands-fishing" compare equal.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip()


def trim_quotes(text: str) -> str:
    """Strip leading/trailing quote and dash punctuation.

    Workers sometimes close a quotation they opened when excerpting reported
    speech. Only edge punctuation is removed; interior characters are untouched.
    """
    return text.strip(" \"'-.,;:")


def grounded(excerpt: str, source_text: str) -> bool:
    """True when ``excerpt`` occurs in ``source_text`` under folding.

    A single contiguous match is the normal case. Workers also legitimately
    quote across a structural boundary in the source markup -- a paragraph
    break and a bracketed line marker such as ``[302]`` -- rendering two
    adjacent passages as one quotation. That is faithful quotation of adjacent
    text, so it is accepted only when the omitted span is short and contains
    nothing but markup, whitespace and a line marker. Skipping actual prose is
    still rejected, because that would let a claim stitch together distant
    passages and change their meaning.
    """
    hay = fold(source_text)
    needle = fold(excerpt)
    if not needle:
        return False
    if needle in hay:
        return True
    trimmed = trim_quotes(needle)
    if trimmed in hay:
        return True
    return _grounded_across_marker(trimmed, hay)


# Text permitted inside an elided span: HTML tags, whitespace, quote marks and
# a bracketed line/section marker. Any prose here means the excerpt skipped
# real content and must not be treated as a faithful quotation.
_ELIDABLE = re.compile(r"^(?:\s|</?[a-zA-Z][^>]*>|[\"'`]|\[\d+[a-z]?\]|[.,;:]|-)*$")
_MAX_ELISION = 40


def _grounded_across_marker(needle: str, hay: str) -> bool:
    """Accept a quotation split by a short, prose-free structural gap."""
    # Longest prefix of the needle that occurs in the source.
    lo, hi, best = 0, len(needle), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if needle[:mid] in hay:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    head, tail = needle[:best].strip(), needle[best:].strip()
    if len(head) < 40 or len(tail) < 20:
        return False
    start = hay.find(head)
    while start != -1:
        gap_from = start + len(head)
        found = hay.find(tail, gap_from)
        if found != -1 and found - gap_from <= _MAX_ELISION:
            if _ELIDABLE.match(hay[gap_from:found]):
                return True
        start = hay.find(head, start + 1)
    return False


def check(claims, snapshot_text_by_source_id):
    """Return the list of claims whose excerpts are not grounded."""
    failures = []
    for claim in claims:
        source_ids = claim.get("source_ids") or []
        text = ""
        for sid in source_ids:
            text = snapshot_text_by_source_id.get(sid, "")
            if text:
                break
        if not text:
            failures.append((claim.get("claim_id"), "no snapshot for source_ids"))
            continue
        if not grounded(claim.get("excerpt", ""), text):
            failures.append((claim.get("claim_id"), "excerpt not found in snapshot"))
    return failures


# --------------------------------------------------------------------------
# Self-test: the gate must accept faithful transcription variants and reject
# fabrications. Run automatically before any real verification.
# --------------------------------------------------------------------------
_SRC = (
    "and whatever might come to their hands\u2014fishing with bent hooks, for hunger "
    "pinched their bellies. She is not mortal, but an immortal bane, dread, and dire, "
    "and not to be fought with; there is no defence. "
    "Scylla seized from out the hollow ship six of my comrades who were the best in "
    "strength and in might. wooing thy godlike wife, and offering wooers&#39; gifts. "
    "And she, as she mournfully answered him."
)

# Mirrors the real snapshot shape: a paragraph break plus a bracketed line
# marker separating two adjacent passages of the same speech.
_SRC_MARKER = (
    "<p>[294] \"but be content to eat the food which immortal Circe gave.' </p> "
    "<p>[302] \"So I spoke; and they straightway swore that they would not "
    "slay the cattle. But when they had sworn, I moored the ship in the harbour. "
    "Then a long while afterward the wind blew from the south, and my men "
    "began to plot mischief among themselves in secret.</p>"
)

_MUST_ACCEPT = [
    # exact
    "Scylla seized from out the hollow ship six of my comrades",
    # space introduced before an em dash
    "come to their hands \u2014fishing with bent hooks",
    # HTML entity in source, plain apostrophe in excerpt
    "offering wooers' gifts",
    # worker closed a quotation it opened
    "wooing thy godlike wife, and offering wooers' gifts.\u201d",
    # typographic vs ascii quoting + reflowed whitespace
    "She is not mortal,   but an immortal bane, dread, and dire",
]

# Adjacent passages joined across only a paragraph break + line marker.
_MUST_ACCEPT_MARKER = [
    "but be content to eat the food which immortal Circe gave. So I spoke; "
    "and they straightway swore that they would not",
]

_MUST_REJECT = [
    # negation deleted -> reverses meaning
    "She is mortal, but an immortal bane, dread, and dire",
    # number changed
    "Scylla seized from out the hollow ship seven of my comrades",
    # clause reordered
    "six of my comrades Scylla seized from out the hollow ship",
    # wholly invented
    "Odysseus slew the Sirens with his bronze spear",
    # plausible-sounding but not in source
    "for hunger pinched their bellies and they wept aloud",
    # empty
    "",
]

# Splices that skip real prose must stay rejected even with marker tolerance.
_MUST_REJECT_MARKER = [
    # jumps over two whole sentences of narrative
    "So I spoke; and they straightway swore that they would not slay the cattle. "
    "Then a long while afterward the wind blew from the south",
    # joins distant passages, inventing an adjacency that changes the meaning
    "but be content to eat the food which immortal Circe gave. my men began to "
    "plot mischief among themselves in secret",
]


def _self_test() -> None:
    errors = []
    for good in _MUST_ACCEPT:
        if not grounded(good, _SRC):
            errors.append(f"false negative (should accept): {good!r}")
    for bad in _MUST_REJECT:
        if grounded(bad, _SRC):
            errors.append(f"FALSE POSITIVE (should reject): {bad!r}")
    for good in _MUST_ACCEPT_MARKER:
        if not grounded(good, _SRC_MARKER):
            errors.append(f"false negative across marker (should accept): {good!r}")
    for bad in _MUST_REJECT_MARKER:
        if grounded(bad, _SRC_MARKER):
            errors.append(f"FALSE POSITIVE across marker (should reject): {bad!r}")
    if errors:
        raise SystemExit("excerpt-grounding self-test FAILED:\n  " + "\n  ".join(errors))


def main() -> int:
    _self_test()
    if len(sys.argv) < 2:
        print(json.dumps({"self_test": "PASS", "accepted": len(_MUST_ACCEPT),
                          "rejected": len(_MUST_REJECT)}))
        return 0
    field_root = pathlib.Path(sys.argv[1]).resolve()
    evidence = field_root / "evidence"
    sources = [json.loads(l) for l in (evidence / "sources.jsonl").read_text().splitlines() if l.strip()]
    claims = [json.loads(l) for l in (evidence / "claims.jsonl").read_text().splitlines() if l.strip()]
    texts = {}
    for row in sources:
        snap = field_root / row["snapshot"]
        texts[row["source_id"]] = snap.read_text(encoding="utf-8", errors="replace")
    failures = check(claims, texts)
    if failures:
        for cid, why in failures:
            print(f"{cid}: {why}", file=sys.stderr)
        print(json.dumps({"claims": len(claims), "ungrounded": len(failures), "valid": False}),
              file=sys.stderr)
        return 1
    print(json.dumps({"self_test": "PASS", "claims": len(claims), "ungrounded": 0, "valid": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
