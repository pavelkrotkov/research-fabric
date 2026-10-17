# Opt-in minimal compilation

The full `ResearchRun` pipeline and CAO adapter are unchanged. Use this separate
command only when source-to-wiki drafts are wanted without structured claims.
Strict completion is an operational gate, not a factual-verification guarantee.
Old evidence ledgers copied from an existing KB are **not reverified or updated**.
`run.json` identifies this mode and records `verified_claims: false`.

## Qualified environment and command

Use Python 3.12+ with the engine and the existing qualified native dependencies:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . -r tests/native-requirements.txt
.venv/bin/python -m research_fabric.minimal \
  --kb /approved/path/existing-kb \
  --source-dir /approved/path/sources \
  --manifest /approved/path/source-manifest.jsonl \
  --run-root /approved/path/new-minimal-run \
  --max-requests 100 --max-tokens 2000000
```

These illustrative paths must be chosen by the operator. Starting the command
authorizes the stated subscription usage; installation or offline tests do not.
The model is deliberately qualified only as **GPT-6 Luna xhigh via ChatGPT**.
It reads the existing `~/.codex/auth.json` into memory. It never copies or writes
credentials, refreshes tokens, starts login, changes model/provider, or uses a
paid API fallback. Refresh an expired sign-in outside the command. Missing or
modified OpenKB 0.4.5/LiteLLM 1.87.2 compiler/authenticator bytes fail closed.

The original KB must be a clean Git repository with publication files tracked,
an existing OpenKB `.openkb/config.yaml`, and no pending native journal or
symlinks. Main is allowed as a **read-only baseline**: all mutations happen in a
self-contained local clone on `codex/minimal`. No remote remains on that clone.
No merge, push, deployed service update, or automatic default change occurs.

The manifest is JSONL, with at least `snapshot` and `sha256` per row. Its **row
order is compilation order**; filenames are not re-sorted. All hashes are checked
before generation and again before accepting each result. Use unique ASCII
basenames/slugs with `.html`, `.md`, or `.txt`. Duplicate source contents/slugs
are rejected. The full reading pipeline remains available for PDF/EPUB extraction
and separately qualified visual interpretation.

## Local image preservation

Markdown and HTML sources may reference local figures. Add an `assets` object to
the source's manifest row, mapping **every active image's normalized relative
path** to the original file's SHA-256, for example:

```json
{"snapshot":"chapter.md","sha256":"<document SHA-256>","assets":{"figures/plot.png":"<image SHA-256>"}}
```

Inline and reference-style Markdown images, titles, URL-encoded filenames,
fragments, and HTML image tags are discovered with the existing source parser.
Code examples and comments are excluded. Missing, extra, changed, escaping or
symlinked assets fail before model usage. Remote URLs, embedded data URIs and
`srcset` are not qualified; provide an explicit local image instead. Preservation
accepts PNG, JPEG, GIF, WebP, SVG, AVIF, BMP, TIFF, PDF and EPS figure files as
opaque bytes. It does not decode, resize, render or certify their readability.
Resources referenced inside opaque vector files are not collected or qualified;
byte preservation does not certify self-contained vector rendering.

Paths are resolved, not rejected just for containing `..`: `a/../figures/plot.png`
is accepted when it resolves inside the source directory. For saved web pages,
resolve website-relative images against the recorded original page URL, freeze
the required bytes locally, and update image links in a prepared derivative.
Retain the immutable original and record both document hashes and asset URLs;
do not treat website-relative references as arbitrary local filesystem access.

Each source and its declared asset closure are staged in a dedicated input
directory. Native conversion remains in use. Small preserves byte-exact copies
beside the raw original and converted source page, verifies native image links
against the corresponding original hashes, and rejects conversion that drops,
adds or substitutes image references. It rejects conflicting existing files;
earlier accepted assets and illustrated source pages cannot be overwritten by
later compilations, including sources/assets inherited from the baseline KB.
Undeclared files copied by native conversion also block acceptance. Receipts and
Git blobs bind the preserved files, and failures
use the same complete candidate rollback as text-only runs.

`wiki/assets/minimal/<source-sha256>.json` records the checked source-page and
image hashes. The existing browser exporter verifies these records and carries
the image files into the exported site, including native wiki-root image links.

**Image interpretation remains NOT_QUALIFIED.** Preservation makes figures
available for navigation and later inspection. This path does not send pixels
to the model or establish that generated prose understands a chart, diagram or
equation. Keep a separate multimodal qualification and budget before claiming
visual grounding. A text-only manifest needs no `assets` field.

## Completion, publication, and rollback

Every source runs in a separate child with the existing qualified strict native
collector. A native `added` message, directory CLI success or hash-registry hit
is not acceptance. Required page writes, the normalized plan, summary and
reciprocal links must have actual completion outcomes. Native optional summary
fallback is recorded separately. There is no automatic model retry.

After the child, the parent verifies the receipt, toolchain, source/output hashes,
wikilinks, local Markdown links and native source metadata targets. It stages
only `wiki/` and `raw/`, rejects ignored outputs and empty proposals, verifies
actual proposed Git blob bytes, and requires a clean worktree. Each source gets
a commit and a receipt bound to input SHA, native report, output tree and parent
commit. Receipts include ignored native state hashes; native state stays private.
Trailing spaces (including Markdown hard line breaks) are permitted without
rewriting compiler output. The command scopes that Git setting to its diff check;
conflict markers and the other default Git diff errors still block publication.

Before every source, the command preserves the complete candidate, including its
Git directory and ignored native state, under `attempt-NNNN/baseline/`. A failed
source's candidate is quarantined as `failed-candidate/`; the previous checkpoint
is restored. Earlier completed sources remain accepted. This deliberately uses
disk copies rather than owning a second database rollback implementation. It
costs disk proportional to retained source attempts; do not use it for huge KBs
without a separately qualified snapshot strategy. All run data is private mode
0700 on the run directory and belongs outside public repositories. Inspect
quarantined native journals as files; their recovery paths refer to the former
candidate location, so recovery must go through this command.

`READY_FOR_REVIEW` is written only after every source has a verified checkpoint.
Review `RUN/candidate` and its Git history. The full pipeline remains available
for rollback to the original architecture; the original KB is never modified.

## Resume and accounting

Repeat the identical command with `--resume`. Settings, code, parser version, manifest, source
hashes, receipts, candidate HEAD and full tree must still match, and the Git index
and worktree must be clean. Completed sources
are skipped only through these receipts, never through the native registry.
A completed run resumes without model calls. An abandoned source is restored
from its verified baseline before retry; quarantine retains the interrupted tree.

The isolated clone may reuse the `codex/minimal` branch name even when the
read-only baseline already has that branch. The baseline's branch, refs, commit
and files stay unchanged. The CLI's post-response output acceptance limit is
32,000 tokens, as qualified in the Odyssey replication. This is a rejection
check after receiving a response, not a provider-side generation limit; the
explicit cumulative request/token budgets still apply.
An interrupted restoration can itself be retried; partial restore copies and
orphan attempts remain quarantined rather than overwriting earlier diagnostics.
The run lock is inherited by the compiler child, so a dead parent cannot allow a
second process to race a still-running child.

`budget.json` and `request-NNNNN.json` contain counts/status/model/effort, not
prompts, response bodies or credentials. Each actual HTTP request is reserved
before sending and reconciled to returned total usage. Requests are serial;
native/provider retries are disabled. Unknown usage keeps a conservative input
UTF-8 byte + framing + 131,072-output-token reservation and blocks continuation.
Authentication, quota, model mismatch, incomplete responses, acceptance-limit
violations and budget exhaustion stop the run even when native code swallows an
optional failure. Editing counters to reset a budget is rejected against records.

The subscription route does not expose a hard output-generation cap. The
32,000-output-token setting is an **acceptance limit**: usage is still charged if
the model exceeds it. Reservations are conservative accounting, not a claim of
provider-enforced cost or mathematical token bounds. A stopped or unknown-usage
run requires operator investigation; `--resume` cannot clear its stop or reset
counters. Do not start another budget to evade the original authorization.
A crash between the budget and request-record writes also blocks continuation
on their integrity mismatch; automatic accounting repair is intentionally absent.
HTTP redirects are never followed: they stop the run with the unsuccessful
attempt's reservation retained, so extra endpoint hops cannot bypass accounting.

## Offline acceptance

Install pytest into the same environment, then run:

```sh
mkdir -p runs/minimal-check/tmp
TMPDIR="$PWD/runs/minimal-check/tmp" LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -m pytest \
  tests/test_minimal.py tests/test_native_compilation.py tests/test_compilation.py \
  --basetemp=runs/minimal-check/pytest -o cache_dir=runs/minimal-check/pytest-cache
```

Tests exercise the real native compiler through synthetic HTTP responses with
network access prohibited, and use disposable local Git repositories. They cover
normal completion, stale registries, missing writes, malformed plans, timeout,
ignored outputs, source/receipt/candidate/budget drift, partial recovery, and
publication checks. The existing native/full-pipeline regressions remain in CI
alongside the new opt-in tests on macOS and Linux. No live corpus/model call is
part of this implementation acceptance. Semantic noninferiority and the academic
PDF trial remain separate gates before changing defaults or deleting modules.

Retrieval stays delegated to QMD. This command creates no search index or new
QMD installation and does not alter shared collections. Index original-derived
source Markdown and generated pages in separate explicitly approved collections;
verify source conversion and retrieval separately before product rollout.
