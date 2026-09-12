# Markdown source contract

Markdown snapshots are first-class immutable sources. A project opts in by matching `.md`/`.markdown` files in `snapshot_pattern` and naming them in `book_label_template`; no Markdown-specific workflow is required.

## Canonical bytes and derived view

The source ledger records two independent hashes:

- `sha256`: the exact immutable source bytes.
- `representation_sha256`: the UTF-8 worker/gate view.

Markdown is decoded as strict UTF-8 and the derived view changes only line endings (`CRLF`/`CR` to `LF`). Front matter, headings, paragraph boundaries, emphasis, links, tables, fenced code, inline/display LaTeX, and image syntax therefore remain textual and locatable. Any source or formula edit changes one or both hashes and fails provenance validation.

## Images and other visual assets

Local Markdown image references (`![alt](path)` and reference-style equivalents) must resolve beneath the snapshot directory. Missing or escaping paths fail closed, and publication copies referenced files beside the Markdown snapshot with the same relative path. Remote image URLs remain references and are not fetched.

The text-only pipeline verifies the Markdown representation, **not image/chart pixels**. Put a caption, transcription, or data description in ordinary Markdown text when visual content must support a claim; that text can then be excerpt-grounded normally. Image alt text and links are preserved, but must not be treated as pixel verification.
