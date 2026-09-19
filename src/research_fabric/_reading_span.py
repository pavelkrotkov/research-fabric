"""Map normalized Markdown spans back to immutable source bytes.

This module owns syntax-aware boundaries and exact quote projection only.
It does not choose reading assignments or infer semantic support.
"""

from __future__ import annotations

import re

from ._source_adapter import _MARKDOWN, _lines


def _frontmatter_end(lines):
    if lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:], 1):
        if line.strip() in {"---", "..."}:
            return index
    return 0


def _math_boundaries(lines, inert):
    blocked, opening = set(), None
    for index, line in enumerate(lines):
        if index in inert or line.strip() != "$$":
            continue
        if opening is None:
            opening = index
            continue
        blocked.update(range(opening + 1, index + 1))
        opening = None
    if opening is not None:
        blocked.update(range(opening + 1, len(lines)))
    return blocked


def _boundaries(lines, tokens, references):
    blocked, inert = set(), set()
    for token in tokens:
        if token.level != 0 or not token.map:
            continue
        blocked.update(range(token.map[0] + 1, token.map[1]))
        if token.type in {"fence", "code_block"}:
            inert.update(range(*token.map))
    for row in references.values():
        blocked.update(range(row["map"][0] + 1, row["map"][1]))
    blocked.update(_math_boundaries(lines, inert))
    blocked.update(range(1, _frontmatter_end(lines) + 1))
    return set(range(len(lines) + 1)) - blocked


def _sections(name, lines, tokens, allowed):
    starts = {0, len(lines)}
    starts.update(t.map[0] for t in tokens if t.type == "heading_open" and t.level == 0 and t.map[0] in allowed)
    points = sorted(starts)
    return {
        f"{name}#L{a + 1}-{b}": {"source_file": name, "lines": [a + 1, b + 1], "disposition": "primary"}
        for a, b in zip(points, points[1:])
    }


def _definitions(tokens, references):
    found = {key: row["map"][0] for key, row in references.items()}
    for token in tokens:
        match = re.match(r"\[\^([^\]]+)\]:", token.content) if token.type == "inline" else None
        if match:
            found["^" + match[1].upper()] = token.map[0]
    return found


def structure(name, rep):
    """Split only at syntax-safe top-level headings and retain definition anchors.

    Markdown parser maps protect fences, paragraphs, reference definitions, and frontmatter.
    """
    lines, env = _lines(rep.text), {}
    if not lines:
        raise ValueError(f"empty reading source: {name}")
    tokens = _MARKDOWN.parse(rep.text, env)
    references = env.get("references", {})
    allowed = _boundaries(lines, tokens, references)
    return _sections(name, lines, tokens, allowed), _definitions(tokens, references), allowed


def span_range(rep, span):
    """Convert a one-based, end-exclusive source line span to representation offsets.

    Invalid or out-of-range spans fail before quote or citation projection.
    """
    bounds = span.get("lines")
    if not isinstance(bounds, list) or len(bounds) != 2 or not all(type(n) is int for n in bounds):
        raise ValueError("invalid reading source line range")
    lines = _lines(rep.text)
    start, end = bounds
    if not 1 <= start < end <= len(lines) + 1:
        raise ValueError("invalid reading source line range")
    return sum(map(len, lines[: start - 1])), sum(map(len, lines[: end - 1]))


def span_text(representations, span):
    rep = representations[span["source_file"]]
    start, end = span_range(rep, span)
    return rep.text[start:end]


def exact_excerpt(text, excerpt):
    """Accept only one literal occurrence inside the already assigned source span.

    Ambiguity is evidence ambiguity and must not be resolved heuristically.
    """
    if not isinstance(excerpt, str) or not excerpt:
        raise ValueError("reading claim has no exact excerpt")
    start = text.find(excerpt)
    if start < 0 or text.find(excerpt, start + 1) >= 0:
        raise ValueError("reading quote is missing, ambiguous, or outside its assigned source spans")
    return start


def reading_source_text(rep, claim):
    if "reading" not in claim:
        return rep.grounding_text
    start, end = span_range(rep, claim["reading"])
    return rep.text[start:end]


def reading_quote(rep, claim):
    """Project current excerpt offsets in normalized and immutable original bytes.

    The original-byte map preserves CRLF and other source-level newline differences.
    """
    start, end = span_range(rep, claim["reading"])
    start += exact_excerpt(rep.text[start:end], claim.get("excerpt"))
    end = start + len(claim["excerpt"])
    return {
        "representation_bytes": [len(rep.text[:start].encode()), len(rep.text[:end].encode())],
        "original_bytes": [rep.original_byte_offset(start), rep.original_byte_offset(end)],
    }

