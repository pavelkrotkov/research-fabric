#!/usr/bin/env python3
"""Generic OpenKB-vault -> MkDocs static-wiki builder.

Converts an Obsidian-style [[wikilink]] markdown vault (the `wiki/` dir inside
an OpenKB field root, e.g. zettelkasten/classics/wiki) into an MkDocs project:
- [[wikilinks]] become plain relative markdown links (no Obsidian plugin needed)
- per-book pages get "Book N" titles; nav is written to preserve natural order
- index.md bullet lists are natural-sorted

This is project-agnostic tooling: the SOURCE vault and site identity are
supplied via CLI flags/env, never hardcoded. The deployed *instance* (which
vault, what it's called, the systemd unit, the built site) lives outside this
repo in ~/services/<instance>. See tools/wiki/README.md.

Usage:
  build_wiki.py --vault /path/to/wiki --site "My KB" --desc "..." [--docs DIR]
  # env fallback: WIKI_VAULT, WIKI_SITE_NAME, WIKI_SITE_DESC
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil

SRC = None
DST = None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def convert(text: str, page_dir: pathlib.Path) -> str:
    def repl(m):
        target = m.group(1).strip()
        # forms: [[path/to/note]], [[note|alias]]
        alias = target
        if "|" in target:
            target, alias = (p.strip() for p in target.split("|", 1))
        # resolve: try as path under wiki/, then by filename match
        cand = SRC / target
        if not cand.suffix:
            cand = cand.with_suffix(".md")
        if cand.exists():
            rel = cand.relative_to(SRC).with_suffix("")
            depth = len(page_dir.relative_to(SRC).parts)
            href = ("../" * depth) + str(rel) + "/"
        else:
            hits = list(SRC.rglob(pathlib.Path(target).with_suffix(".md").name))
            if hits:
                rel = hits[0].relative_to(SRC).with_suffix("")
                depth = len(page_dir.relative_to(SRC).parts)
                href = ("../" * depth) + str(rel) + "/"
            else:
                return f"**{alias}**"
        return f"[{alias}]({href})"

    return re.sub(r"\[\[([^\]]+)\]\]", repl, text)


def book_sort_key(rel):
    """Natural sort: summaries/odyssey-book-2.md before odyssey-book-10.md."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(rel))]


def write_mkdocs_yml(site_name: str, site_description: str):
    """Generate mkdocs.yml with an explicit nav so pages keep natural order."""
    sections = [
        ("Home", ["index.md", "log.md"]),
        ("Summaries", sorted((DST / "summaries").rglob("*.md"), key=book_sort_key)),
        ("Concepts", sorted((DST / "concepts").rglob("*.md"), key=book_sort_key)),
        ("Entities", sorted((DST / "entities").rglob("*.md"), key=book_sort_key)),
        ("Sources", sorted((DST / "sources").rglob("*.md"), key=book_sort_key)),
    ]
    nav = []
    for title, files in sections:
        entries = []
        for f in files:
            if isinstance(f, str):
                rel = f
                stem = f.rsplit("/", 1)[-1].removesuffix(".md")
            else:
                rel = f.relative_to(DST).as_posix()
                stem = f.stem
            name = stem.replace("-", " ").title()
            m = re.search(r"book[- ]?(\d+)$", stem)
            if m:
                name = f"Book {int(m.group(1))}"
            elif stem == "index":
                name = "Index"
            elif stem == "log":
                name = "Log"
            entries.append({name: rel})
        if entries:
            nav.append({title: entries})
    import yaml  # available via mkdocs' dependency chain

    cfg = {
        "site_name": site_name,
        "site_description": site_description,
        "theme": {
            "name": "material",
            "features": [
                "navigation.instant",
                "navigation.tracking",
                "navigation.indexes",
                "navigation.sections",
                "search.highlight",
            ],
        },
        "use_directory_urls": True,
        "markdown_extensions": [
            "admonition",
            {"toc": {"permalink": True}},
            "meta",
        ],
        "nav": nav,
    }
    out = DST.parent / "mkdocs.yml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


def sort_index_page(text: str) -> str:
    """Natural-sort bullet lists in index.md, keeping headings/prose in place."""
    lines = text.splitlines()
    out, block = [], []

    def flush():
        if not block:
            return

        def key(line):
            m = re.search(r"\[\[([^\]|]+)", line)
            target = m.group(1).strip() if m else line
            return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", target)]

        out.extend(sorted(block, key=key))
        block.clear()

    for line in lines:
        if re.match(r"^\s*-\s*\[\[", line):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def main():
    global SRC, DST
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("WIKI_VAULT"), help="path to the source OpenKB wiki/ vault")
    ap.add_argument("--site", default=os.environ.get("WIKI_SITE_NAME", "Research KB"), help="MkDocs site_name")
    ap.add_argument(
        "--desc",
        default=os.environ.get("WIKI_SITE_DESC", "Evidence-backed knowledge base"),
        help="MkDocs site_description",
    )
    ap.add_argument("--docs", default=None, help="output docs dir (default: <script_dir>/site-src/docs)")
    args = ap.parse_args()
    if not args.vault:
        raise SystemExit("build_wiki.py: --vault is required (or set WIKI_VAULT)")
    SRC = pathlib.Path(args.vault).resolve()
    DST = (
        pathlib.Path(args.docs).resolve()
        if args.docs
        else pathlib.Path(__file__).resolve().parent / "site-src" / "docs"
    )

    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)
    n = 0
    for md in SRC.rglob("*.md"):
        rel = md.relative_to(SRC)
        dest = DST / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            convert(
                sort_index_page(md.read_text(encoding="utf-8"))
                if rel.name == "index.md"
                else md.read_text(encoding="utf-8"),
                md.parent,
            ),
            encoding="utf-8",
        )
        n += 1
    print(f"converted {n} notes")
    write_mkdocs_yml(args.site, args.desc)
    print("wrote mkdocs.yml with natural-order nav")


if __name__ == "__main__":
    main()
