# EPUB sources

EPUB is an optional source format. The adapter uses only the Python standard library and treats the EPUB container as immutable input: provenance records the original EPUB SHA-256, adapter/version, UTF-8 extracted-representation SHA-256, and deterministic package metadata (`title`, `language`, `identifier`, package version/path, and spine count).

Extraction follows the OPF spine exactly. Each XHTML spine item emits stable `@@chapter <path>` and `@@section <path>#<id-or-heading-ordinal>` locators. Text is extracted in document order; MathML contributes its textual content after an `@@formula` marker. Embedded image references emit `@@asset <path>` markers. Missing package/manifest resources, duplicate archive entries/resources/spine IDs, duplicate spine content, unsupported non-XHTML spine items, unsafe/external paths, malformed ZIP/XML, DTD/entity declarations, excessive XML nesting, and excessive ZIP expansion fail closed.

The representation is text evidence only. `@@asset` proves that an image is referenced and bound by the original EPUB bytes; it does **not** verify pixels, charts, diagrams, OCR, or image-only claims. Such claims need a separately reviewable caption/transcription or a future image-aware adapter. MathML extraction likewise preserves textual formula content, not rendered layout or visual semantics.

The checked-in golden EPUB fixtures pin spine order, headings, MathML, asset references, package metadata, and the exact extracted representation so identical bytes/toolchain produce byte-identical output.
