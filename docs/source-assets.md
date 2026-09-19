# Source visual bundles

Markdown assets extend the existing source adapter and `assets_sha256` attestation.
The immutable original source and aggregate original-asset digest remain the root;
`prepare_source_bundle(source, destination, attestation)` rejects changed originals
before building a source-scoped bundle. The scope hashes the portable source
attestation, including the asset closure, so identical document names/text with
different figures cannot overwrite each other. Absolute host paths are not identities.

The Markdown adapter (version 2) uses CommonMark tokens and HTMLParser for Markdown
images, reference definitions, HTML `img src`, `embed src`, and `object data`.
Comments, fenced/indented/inline code and raw HTML pre/code/script/style examples
are inert. Reference records retain syntax, original target including fragment,
caption, and one-based, end-exclusive source block line ranges. These are block
locators, not invented pixel regions or exact inline character offsets.

Local paths must remain inside the source directory after URL decoding and symlink
resolution. Absolute paths and traversal fail. Missing original bytes fail even
for optional visuals: optionality never weakens the source manifest. Remote
references are never downloaded. `srcset` is deliberately unsupported. Required
remote/unsupported/unreadable visuals fail preparation; an HTML visual explicitly
marked `data-optional="true"` records a machine-readable limitation instead.
Optional local unsupported files retain a usable original link. Markdown image
syntax is required by default; use the explicit HTML form for optional figures.

`bundle.json` records originals, their per-file hashes, raster derivatives,
derivative hashes, renderer versions/configuration, prepared-input hash and source
attestation. Supported raster inputs are PNG/JPEG/GIF/WebP (first frame). Pillow
loads the image to reject unreadable/truncated data and caps it at 16 million
pixels. PDF uses `pdftoppm`; EPS uses Ghostscript in `-dSAFER` mode. Executables are
discovered on PATH. Both render only the complete first page, at most 1600 pixels
per dimension, with a 30-second timeout and 20 MB input/output file limit. EPS has
a fixed canvas with fit-to-page; PDF preserves aspect ratio. Non-first-page vector
fragments fail (or become optional limitations). Tool stdout/stderr is discarded
during rendering to avoid unbounded diagnostic accumulation. No shell, source
script launcher or remote fetch is used. These limits do not promise an OS-level
memory sandbox; run untrusted corpora in the deployment's process sandbox.

Rendering bytes can depend on the toolchain. Resume verifies the frozen bundle,
source/derivative bytes and renderer version/configuration, and fails if anything
changed or disappeared. It does not silently regenerate a missing derivative.
Asset paths/hashes identify available originals and derivatives; renderer records
identify rendered derivatives. Inspection and review records belong to issue #14,
which consumes derivative path/hash plus source locator. This module makes no claim about equation transcription or interpretation.

OpenKB 0.4.5's native Markdown image copier uses a regex even inside code/comments.
The derived input replaces only parsed image references and visual HTML tags with
figure markers, then emits canonical validated images in a mapped figure gallery.
The same adapter HTML policy identifies active tags; source positions map edits
back through list/quote prefixes without rendering the document. Image-shaped
examples have only their leading exclamation mark entity-escaped to defeat the
native regex. All other text, math, tables, fences, raw HTML and line endings stay
unchanged. Gallery captions use literal fenced blocks so LaTeX and operators remain
exact while decoded HTML attributes cannot introduce new active tags. Documents without references or image-shaped examples stay byte-exact. Original Markdown remains byte-exact in snapshots, and
its existing text representation remains authoritative for evidence grounding.
Prepared input filenames use the scoped bundle identity; native source/summary
names are carried through the workflow's note mapping. Inputs are passed to native
OpenKB individually so bundled PDFs are not accidentally ingested as documents.
No native compiler or converter is replaced.

After native conversion, `publish_bundle` verifies native image hashes and copies
original assets beside a byte-identical copy of `bundle.json`. There is no separate
publication receipt or independently recorded native document name: the prepared
input stem determines it, and publication verifies the native name against that
stem. The existing wiki exporter consumes the same manifest's output mapping, rechecks
hashes and copies only its referenced originals and native raster outputs. It
adjusts native vault-root image paths to browser page-relative paths; it does not
implement a second Markdown/HTML asset resolver. Existing source snapshots are
scoped identically in production and offline rehearsal. Rehearsal still does not
compile; it uses an already compiled field and the native note identity derived
from the published bundle.
The eventual shared publication extraction belongs to issue #18.

Validation: `uv run pytest` exercises parsers, paths, collisions, renderer failures,
real local PDF/EPS rendering, optional limitations and resume/export drift. Real
renderer tests skip explicitly if their executable is absent. Run
`python tests/native_asset_smoke.py` with OpenKB 0.4.5 installed plus `pdftoppm` and
`gs` for real prepare → native conversion → existing exporter validation. It uses
two colliding synthetic works, fresh temporary trees and no model/network calls.
EPUB pixels and standalone PDF ingestion are outside this Markdown slice.
