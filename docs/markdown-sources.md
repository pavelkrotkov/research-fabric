# Markdown source contract

Markdown snapshots are first-class immutable sources. A project opts in by matching `.md`/`.markdown` files in `snapshot_pattern` and naming them in `book_label_template`; no Markdown-specific workflow is required.

## Canonical bytes and derived view

The source ledger records exact provenance for each source representation:

- `sha256`: the exact immutable source bytes.
- `representation_sha256`: the UTF-8 worker/gate view.
- `assets_sha256`: for Markdown with local assets, a digest of each referenced path and file digest.

Markdown is decoded as strict UTF-8 and the derived view changes only line endings (`CRLF`/`CR` to `LF`). Front matter, headings, paragraph boundaries, emphasis, links, tables, fenced code, inline/display LaTeX, and image syntax therefore remain textual and locatable. Any source, formula, or referenced local-asset edit changes provenance and fails validation.

## Images and other visual assets

Local Markdown image references (`![alt](path)` and reference-style equivalents) must resolve beneath the snapshot directory. Missing or escaping paths fail closed, and publication copies referenced files beside the Markdown snapshot with the same relative path. Remote image URLs remain references and are not fetched.

The text-only pipeline verifies the Markdown representation and asset bytes, **not image/chart meaning**. Put a caption, transcription, or data description in ordinary Markdown text when visual content must support a claim; that text can then be excerpt-grounded normally. Image alt text and links are preserved, but must not be treated as pixel-content verification.
