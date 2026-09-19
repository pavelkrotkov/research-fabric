"""Surgical native image normalization; never render the source document."""

import re

from markdown_it import MarkdownIt
from markdown_it.rules_inline import html_inline, image

from ._source_adapter import _VisualHTML

# OpenKB 0.4.5 scans this syntax even in fenced code and HTML comments.
_NATIVE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _lines(text):
    # CommonMark lines end at CR/LF, not at Unicode paragraph separators.
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)[:-1]


def _positioned(rule):
    def parse(state, silent):
        start = state.pos
        matched = rule(state, silent)
        if matched and not silent:
            state.tokens[-1].meta["source_span"] = (start, state.pos)
        return matched

    return parse


def _source_positions(content, lines, start):
    """Map parser-stripped container prefixes back to original character offsets."""
    positions = []
    for fragment, line in zip(_lines(content), lines):
        body = fragment.rstrip("\r\n")
        column = line.find(body)
        if column < 0:
            raise ValueError("cannot map visual reference to original source")
        positions.extend(range(start + column, start + column + len(body)))
        if fragment.endswith("\n"):
            positions.append(start + len(line) - 1)
        start += len(line)
    if len(positions) != len(content):
        raise ValueError("incomplete visual source position mapping")
    return positions


def _block_edits(token, html_parser):
    if token.type == "html_block":
        return html_parser.visual_edits(token.content)
    edits = []
    for child in token.children:
        span = child.meta.get("source_span")
        if child.type == "image" and not html_parser.inert:
            edits.append((*span, "[Figure]"))
        elif child.type == "html_inline":
            edits.extend((span[0] + a, span[0] + b, value) for a, b, value in html_parser.visual_edits(child.content))
    return edits


def normalize_images(text):
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.at("image", _positioned(image))
    parser.inline.ruler.at("html_inline", _positioned(html_inline))
    lines = _lines(text)
    edits = []
    html_parser = _VisualHTML()
    for token in parser.parse(text):
        if token.type not in ("inline", "html_block"):
            continue
        local = _block_edits(token, html_parser)
        if not local:
            continue
        first, last = token.map
        positions = _source_positions(token.content, lines[first:last], sum(map(len, lines[:first])))
        edits.extend((positions[a], positions[b - 1] + 1, value) for a, b, value in local)
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    # Only image-shaped examples need escaping. Math, factorials, backslashes,
    # tables and HTML elsewhere stay exactly as the author wrote them.
    return _NATIVE_IMAGE.sub(lambda match: "&#33;" + match.group()[1:], text)


def literal_caption(caption):
    """Literal captions keep scientific notation and cannot activate HTML tags."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", caption)), default=0))
    caption = _NATIVE_IMAGE.sub(lambda match: "&#33;" + match.group()[1:], caption)
    return f"{fence}text\n{caption}\n{fence}"
