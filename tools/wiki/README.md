# tools/wiki — generic OpenKB-vault → MkDocs static-wiki builder

Converts an OpenKB field vault (the `wiki/` dir inside `zettelkasten/<field>/`)
into an MkDocs static site:

- `[[wikilinks]]` → plain relative markdown links (no Obsidian plugin required)
- per-book pages named "Book N"; explicit nav preserves natural order
- `index.md` bullet lists natural-sorted

## Usage

```bash
python3 tools/wiki/build_wiki.py \
  --vault /home/pavel/zettelkasten/classics/wiki \
  --site  "Odyssey Research KB" \
  --desc  "Evidence-backed knowledge base covering all 24 books of the Odyssey" \
  --docs  /path/to/instance/site-src/docs
# then: (cd /path/to/instance/site-src && mkdocs build)
```

Env fallbacks: `WIKI_VAULT`, `WIKI_SITE_NAME`, `WIKI_SITE_DESC`.

## What lives here vs. what stays an "instance"

This directory holds the **generic builder only** — project-agnostic tooling.

The **deployed instance** (which vault, its site name/description, the systemd
unit, the generated `site/`) lives in `~/services/<instance>/`, e.g.
`~/services/odyssey-wiki/`. Its `rebuild.sh` calls this builder with that
instance's arguments and runs `mkdocs build`.

Rationale: the builder is shared executable machinery (belongs with the engine,
this repo). The instance is a deployment of that machinery for one KB (does not
belong in the pipeline repo — see the README's llm-wiki comparison). The
generated site is a build artifact and is never committed.

## Dependencies

- `python3` + `pyyaml` + `mkdocs` (material theme) in the instance's venv.