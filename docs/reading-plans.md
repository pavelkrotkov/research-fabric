# Source-mapped research reading

An explicit `reading` project profile assigns Markdown snapshots to intellectual
works. Source preparation freezes one `reading-plan.json` before model calls.
Workers read its primary and context sections; native OpenKB still compiles one
whole prepared source bundle per snapshot using its stock planner. Native pages
are generated from source inputs independently of the evidence ledger.

## Vocabulary

- **Work:** an intellectual work, with source-backed title/author attribution or
  explicit unknowns. Multiple snapshots of one work share its independence group.
- **Source snapshot:** immutable original bytes, including newline conventions,
  and the original manifest digest. Assets remain in the existing source bundle.
- **Representation:** the adapter's exact worker-readable view and its separate
  digest. Markdown normalizes newlines; original byte offsets remain recoverable.
- **Reading packet:** a frozen input assignment, identified independently of its
  source, with one parent work and separately mapped primary/context sections.
- **Evidence packet:** the existing structured worker output, with stable claim
  IDs, accepted source bindings, execution records, and transition history.

## Setup and project profile

Install the optional dependency and explicitly provision official tokenizer data:

```sh
uv sync --extra reading
export TIKTOKEN_CACHE_DIR=/absolute/path/to/reading-tokenizer-cache
uv run python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
```

Provisioning downloads the official encoding. Reading preparation itself verifies
its pinned SHA256 before loading; missing or corrupt data fails without downloading.
Development and CI install the same `reading` extra. Counts are `cl100k_base` BPE
policy units, **not actual provider/model usage**. The frozen record includes the
encoding, package version, Python runtime, data hash and counting semantics.

Example project YAML (paths are relative to the supplied source directory):

```yaml
corpus_dir: research
manifest_path: source-manifest.jsonl
reading:
  sources:
    chapter/source.md: chapter
    papers/one/source.md: paper-one
    papers/two/source.md: paper-two
  works:
    chapter: {title: null, authors: null}
    paper-one: {title: null, authors: null}
    paper-two: {title: null, authors: null}
  policy:
    target_tokens: 6000
    soft_limit_tokens: 12000
    context_budget_tokens: 2000
acceptance:
  min_claims_per_reading: 1
```

The manifest must retain the existing required source fields and explicitly name
`source_file`, including nested directories. A source ID cannot identify two files.
Do not infer bibliographic authorship from filenames. Non-null titles/authors require
`bibliography_evidence` entries with `source_file` and one-based, end-exclusive
`lines: [start, end]` containing the stated values. Attribution still requires human
interpretation; substring presence alone cannot establish authorship.

Legacy HTML/witness project profiles remain unchanged. The source-mapped reading
profile currently requires Markdown; the optional EPUB textual adapter does not
imply native EPUB ingestion or original-byte section mapping.

## Frozen assignments and overrides

Headings define coherent sections. CommonMark block and reference-definition maps
protect fences, tables, paragraphs and multiline references; display math is kept
intact. Oversized sections stay whole. Each reading records separate primary/context
token counts; compare them with the frozen soft limits to identify excess. These
limits are starting hypotheses, not hard acceptance thresholds.

The plan stores source/representation hashes once and references its authoritative
section table. Every section has `primary`, `context`, or `excluded` disposition;
non-primary dispositions require a reason. Primary coverage has exactly one owner.
Repeated headings use source-relative line identities. Referenced definitions and
explicit work `context: [section-id, ...]` entries remain separately mapped.

An optional `reading.reviewed_plan` JSON path is source-root-relative (or absolute).
It supplies `sections` and `readings` in the frozen schema and may change boundaries,
but cannot split coherent blocks, introduce coverage gaps or overlap assignments.
Reading IDs use 1–128 ASCII letters, digits, `_` or `-`, beginning with a letter or
digit; they are the actual worker identities, not sanitized filename aliases.

Existing frozen plans take precedence over newly edited override files. The research
question is frozen too and drives the worker prompt. Question, source,
work, policy or tokenizer changes require a new run; they cannot replace the frozen
plan during resume. Accepted claims bind its hash, reading ID, cited work, role and
assigned source lines. Repair retains that assignment, including for dropped claims,
and recomputes quote coordinates after excerpt changes. Source paths and multiline
quotes bypass legacy terminal whitespace cleanup.

Context retains its cited work identity but has `independence_group: null`; it does
not satisfy primary-claim minimums. Primary evidence groups by Work, not reading ID.

## Native citations and byte preservation

The isolated KB's supported `wiki/AGENTS.md` receives an idempotent compact mapping:
section ID, heading, original source target, line range and byte range. It does not
copy the corpus or replace native planning. Generated section citations resolve
through `ReadingPlan.section`; accepted quotes use the same source span mapping
and narrow it to exact original bytes. Generated packet headers are never evidence.
This provides mechanical traceability, not proof that a page's reasoning is supported;
broader compiled-wiki review remains separate.

Publication preserves the frozen plan and explicit source-file identities. Original
snapshot, bundle-original, native `raw/` input-copy and `wiki/sources/` converted-source paths receive scoped tracked Git attributes disabling
text conversion and whitespace lint for those data bytes. OpenKB copies prepared inputs to `raw/` and writes converted source views to `wiki/sources/`; neither is model-generated prose. Generated pages keep
normal whitespace checks. `.gitattributes` and native raw inputs are included in commit-byte checks.

The qualified native test exercises a synthetic chapter and two papers through real
worker dispatch, native compilation, ledger gates, proposed Git commit, citation
resolution and disposable rehearsal. Only model transport replies are recorded:

```sh
TIKTOKEN_CACHE_DIR=/absolute/path/to/cache uv run pytest tests/test_reading_plans.py
# In the qualified OpenKB/native environment:
python -m pytest tests/test_native_compilation.py -k source_mapped
```
