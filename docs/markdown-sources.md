# Markdown source contract

Markdown snapshots are first-class immutable sources. A project opts in by matching `.md`/`.markdown` files in `snapshot_pattern` and naming them in `book_label_template`; no Markdown-specific workflow is required.

## Canonical bytes and derived view

The source ledger records exact provenance for each source representation:

- `sha256`: the exact immutable source bytes.
- `representation_sha256`: the UTF-8 worker/gate view.
- `assets_sha256`: for Markdown with local assets, a digest of each referenced path and file digest.

Markdown is decoded as strict UTF-8 and the derived view changes only line endings (`CRLF`/`CR` to `LF`). Front matter, headings, paragraph boundaries, emphasis, links, tables, fenced code, inline/display LaTeX, and image syntax therefore remain textual and locatable. Any source, formula, or referenced local-asset edit changes provenance and fails validation.

## Images and other visual assets

Local Markdown image references (`![alt](path)` and reference-style equivalents) must resolve beneath the snapshot directory. Missing or escaping paths fail closed, and publication copies referenced files beside the Markdown snapshot with the same relative path. Remote images are not fetched; required remote visuals block native preparation, while explicitly optional HTML visuals retain a limitation.

The text-only pipeline verifies the Markdown representation and asset bytes, **not image/chart meaning**. Put a caption, transcription, or data description in ordinary Markdown text when visual content must support a claim; that text can then be excerpt-grounded normally. Image alt text and links are preserved, but must not be treated as pixel-content verification.

Asset-bearing Markdown requires `assets_sha256` in the trusted input manifest,
computed when the corpus is captured (available from `representation_for(path)`).
Binding checks that digest before adding representation fields; it never blesses
unpinned asset bytes. Published Markdown rows require representation metadata,
and worker packet attestations include the asset digest so asset drift invalidates
reuse. Existing metadata-free legacy HTML and text rows retain their policy.

Publication preserves assets in source-scoped evidence snapshots and validated
[compiler/browser bundles](source-assets.md). Only prepared documents are passed
to native compilation; their referenced derivatives remain available beside them,
and bundled PDF/EPS originals are never ingested as separate documents.

## Native compatibility check and limits

With OpenKB 0.4.5 installed, run `python tests/native_markdown_smoke.py`.
It initializes a disposable KB, converts a synthetic Markdown formula through
native `add_single_file`, executes the real short-document compiler, and checks
its source, summary, index and hash registry. Only external model replies are
scripted (a summary and a valid empty concept plan); no compiler result is mocked.
The qualified `tests/test_native_compilation.py` workflow fixture additionally
executes Markdown with an image through fresh extraction and attested reuse, real
native compilation, deterministic gates, a clean commit and `READY_FOR_REVIEW`.
It verifies stable claim/execution records, browser assets and rehearsal against
the production-compiled KB. External model responses and CAO transport/advice are
scripted; live provider or CAO service execution is not claimed.

Grounding reports record the original document filenames from their source
ledger so repair hashes the same document set even when the source directory
also contains assets. Packet attestations separately reject changed asset bytes.
Legacy reports without filenames retain their strict whole-directory check.
