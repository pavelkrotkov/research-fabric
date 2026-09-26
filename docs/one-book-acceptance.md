# Opt-in one-book acceptance

Status for *Business Data Science (2019)*: **NOT_READY — live qualification not
performed**. No operator-supplied book path, account qualification receipt, or
explicit model/budget selection accompanied this implementation. The synthetic
fixture below is not a substitute. Do not launch the rest of the corpus.

The first native CI execution also found an integration blocker before evidence
dispatch: an oversized raster's inspection records the original bytes as its
derivative, while the frozen bundle records a downsampled derivative. Reading
context correctly rejects this identity mismatch. The one-book regression is
currently a failing reproduction, not an offline qualification receipt. See
[the exact-head macOS run](https://github.com/pavelkrotkov/research-fabric/actions/runs/36239571406/job/108397416694)
at `ddccd2a92ed97ea9f5c65b5be411f6d9d9f1d781` (67 other tests passed, one live
test skipped; Linux was cancelled by matrix fail-fast). Retain the hash check and
oversized fixture; resolve the supported inspection/derivative contract first.

## Offline rehearsal (ordinary CI, no login or quota)

Use Python 3.12+ with the unchanged pins in `tests/visual-requirements.txt`, this
project's `reading` extra and pytest. Provision the official tokenizer once as
in [reading-plans.md](reading-plans.md). Then:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -vv \
  tests/test_one_book_launcher.py tests/test_one_book_acceptance.py \
  tests/test_native_compilation.py -k 'one_book or resume_switch or source_mapped'
```

`tests/fixtures/one_book/` is an original MIT-licensed two-chapter measurement
book, not copied from the target book. The test creates a schematic 6000×3000
raster locally, freezes `evaluation.json` before generation and exercises the
production source plan, research workers, bounded native compiler, deterministic
gates, shared publisher, isolated proposed commit, shared rehearsal and browser
exporter. Both Linux and macOS run this test; rehearsal uses the platform's
normal temporary root and an explicit symlink-root regression.

Only external model replies are scripted: native planners/writers, image tools,
source derivatives, gates and Git execute. The visual note is deliberately
uncertain. Assertions cover original/derivative identity, delivery to reading
workers, non-delivery to the native compiler, unreviewed status, unchanged source
bytes, one Work, all section dispositions and citation presence, qualifier
retention after later units, clean proposal/main preservation and exported assets.
The companion existing resume tests exercise failed requests/accepted compile
prefixes, model switch, stable packets and cumulative charges. The replay uses
mixed explicitly scripted routes; it does **not** qualify a live subscription.
OAuth transport replays are separately in `tests/test_oauth_execution.py`.

The exporter produces a MkDocs project, not a rendered/browser-reviewed site.
Building/serving that project and inspecting it is a separate live acceptance step.
No page-count threshold or canned semantic PASS is used.

## Prepare privately, before generation

Keep the licensed book, figures, account storage, logs, derived wiki and evaluation
outside this public checkout. Never attach them to an issue/PR or enable prompt,
image or provider-error logging. Do not copy credentials into a run directory.

1. Supply only the selected book's Markdown conversions and their relative assets
   in a private `sources/` directory. Preserve the original corpus and baseline.
   Create a separate native OpenKB KB (or private clone of the baseline) and a
   clean non-main proposal branch. Do not run in the existing baseline worktree.
2. Supply a private reading project and immutable `source-manifest.jsonl`, following
   [reading-plans.md](reading-plans.md). Copy the manifest to the private run root.
   Assign every snapshot to exactly one Work. Record source-backed title/authors
   with bibliography spans or explicit nulls; directory/filename attribution is
   not evidence. Select `reading.policy` and `compilation.max_request_tokens`
   explicitly, and use coverage mode. Historical token/reading counts from #41
   are descriptive planning measurements, not expected counts or target quality.
3. Before any model call, write an evaluation JSON using the fixture's format:
   `sampling`, `visual_omissions`, and unique `items` with `id`, `source_file`,
   one-based/end-exclusive `lines`, and a literal `expected` source substring.
   Include every chapter, its important definitions and assumptions, worked
   examples, qualifications, conflicting statements and cross-concept connections.
   Sample equations and figures with original locators and asset identities.
   Explain which chapters/visuals are sampled more heavily and why; document
   visual-only propositions that cannot be text-grounded. Review these choices
   before generation. The launcher checks literal spans and snapshot coverage;
   it cannot decide whether sampling is scientifically adequate.
4. Establish the existing native LiteLLM ChatGPT login outside this workflow.
   Select a supported model/effort and qualify current account entitlement with
   the explicitly opt-in one-request `tests/subscription_smoke.py` procedure in
   [execution-profiles.md](execution-profiles.md). That request has its own budget
   and journal: include it in pilot-wide measured usage. An offline transport
   replay is not proof of live entitlement. Never silently fall back to an API key.
5. Record dependency versions and select **all** cumulative and per-call budgets.
   Reserve headroom for native operations, repairs and retries; a reading costs
   more than one request. Budgets count known usage, not a hard monetary ceiling.
   Missing usage remains unknown. The provider strips generation output caps on
   this subscription route: output tokens are an acceptance bound, not a guarantee
   of bounded remote generation. Preparation/dynamic request overflow must stop,
   not truncate the book or replenish quota.

## Launch (explicitly spends subscription quota)

Create private `pilot.json` with exactly these fields. Values below are illustrative,
not an approved live budget or a claim that the book fits:

```json
{
  "project_path": "/private/pilot/book.yaml",
  "field_root": "/private/pilot/proposal-kb",
  "run_root": "/private/pilot/run",
  "source_dir": "/private/pilot/sources",
  "question": "Explain the book's methods, assumptions, examples, qualifications and connections with source locators.",
  "evaluation_path": "/private/pilot/evaluation.json",
  "execution": {
    "subscription_only": true,
    "roles": {
      "extraction": {"provider":"chatgpt","auth":"oauth","account":"default","model":"gpt-6-astra","effort":"medium"},
      "repair": {"provider":"chatgpt","auth":"oauth","account":"default","model":"gpt-6-astra","effort":"medium"},
      "compile": {"provider":"chatgpt","auth":"oauth","account":"default","model":"gpt-6-astra","effort":"medium"}
    },
    "fallbacks": {"extraction":[],"repair":[],"compile":[]},
    "budget": {"requests":100,"seconds":7200,"tokens":2000000,"attempt_seconds":300,"attempt_tokens":100000,"output_tokens":20000,"attempts":1}
  }
}
```

All paths are absolute. Do not add credentials or arbitrary settings to this JSON.
With the qualified interpreter from above:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True python bin/one_book.py /private/pilot/pilot.json --live
```

Without `--live` the command refuses before reading the config or writing files.
It freezes `acceptance-evaluation.json` against actual original source bytes,
appends toolchain/configuration receipts in `acceptance-launches.jsonl`, and calls
`ResearchRun` without an alternate generator or publisher. It never merges, hosts,
uploads, or discovers the full corpus. `acceptance-status.json` deliberately stays
NOT_READY after mechanical publication until the independent operator audit.

Current integrated subscription-only mode disables planning/advisory model calls,
visual preflight and native knowledge lint. `model-stages.json` inventories these
omissions; extraction, repair and compilation alone use the journaled route.
Textual captions and bounded source-figure derivatives remain available, but
inspection prose is **not** delivered through the native compile context path.
Do not claim automated visual qualification. Human inspection of selected originals
and derivatives must be recorded separately. Adding optional model inspection or
advisory calls would require a separately qualified, explicitly budgeted route;
this launcher does not enable those calls or pretend their usage is included.

## Checkpoint recovery and model switch

At a completed failed request or accepted-unit checkpoint, retain the entire run,
source manifest, evaluation, evidence and journal. Preserve the failed candidate.
If the proposal is dirty, use a new clean isolated worktree with the same baseline.
Explicitly select the alternative supported model/effort in the private config and
run the same command with `--live --resume`. Do not modify source, question or
frozen evaluation. Budget increases, if authorized, must be explicit cumulative
limits; the old usage remains charged. Never delete or reset `execution.sqlite3`.

Compare accepted packet bytes, claim IDs, Work identity, original packet execution
records, failed outcomes and cumulative charges before/after. Confirm missing or
failed compile units were actually retried, not skipped by native hash deduplication.
Inspect `verification/compile/units/checkpoint.json` and `completed.json`.
A pending journal entry after abrupt process death is unsupported recovery: stop,
retain diagnostics and obtain a separate run/budget decision. Do not clear entries
to manufacture successful recovery. A successful no-op rerun is not this experiment.

## Export and review without touching the baseline

```sh
python bin/dryrun_publication.py /private/pilot/run /private/pilot/proposal-kb \
  --projects-dir /private/pilot --project book --branch agent/book-pilot
python tools/wiki/build_wiki.py --vault /private/pilot/proposal-kb/wiki \
  --docs /private/pilot/browser/docs --site 'Private one-book review'
# Use the existing browser tooling instructions, with loopback binding only:
# tools/wiki/README.md; build and serve the generated mkdocs.yml locally.
```

The rehearsal needs the original `sources/` copy inside the run; copy it byte-exact
from the supplied snapshots if the run's source directory lives elsewhere. It uses
the same publication implementation without any model calls or input mutation.
Keep the baseline/main refs unchanged and verify the proposal's `git status
--porcelain` is empty. Record the proposed SHA and source hashes. Do not push a
private generated KB to this public repository.

## Operator audit and explicit decision

Write a private report binding the evaluation checksum, source/reading/compile
plan hashes, proposed commit, baseline identity (or unavailable), execution journal,
export path and review date/reviewer. Start with NOT_READY and document:

| Check | Required evidence, not a proxy |
| --- | --- |
| Actual route | Every attempt's requested/returned model, provider/auth/account selector, requested/reported effort, settings, toolchain and outcome; unknown returned values stay unknown. Account entitlement smoke plus `model-stages.json`; no API-key or unaccounted optional call. |
| Measured usage | Sum actual journal requests, known tokens, unknown-usage calls, failed/repair/compile calls and elapsed time across launch revisions. Separately include smoke/any authorized external review calls. No inferred billing from tokenizer counts. |
| Complete dispositions | Compare every frozen section with `evidence/coverage.json`, `compile-plan.json` and `unit-outcomes.json`; exclusions/context-only sections need reasons. Compilation success is only mechanical. |
| Evaluation coverage | For every predeclared ID: original locator, relevant claim IDs and wiki pages, missing/partial/supported/contradicted/unsupported result, reviewer explanation. Open the actual original spans, not just links. |
| Invented content | Reverse-audit generated assertions as well as source-to-wiki coverage. Sample beyond predeclared items, label that sample separately, and flag unsupported deductions, altered assumptions and overstated certainty. |
| Math and visuals | Compare sampled equations/symbols/qualifiers with original lines/images; distinguish originals, downsampled derivatives and unreviewed model notes. Record omissions and visual-only unsupported claims. No exhaustive pixel-verification claim. |
| Citation/link integrity | Inspect exact `quote_span.original_bytes`, section targets and browser links/assets. Confirm original bytes and original/derived image checksums; byte grounding does not establish semantic entailment. |
| Retention | Inspect unit diffs and before/after hashes for earlier qualifiers, connections and contradictions after subsequent native updates. A larger final wiki does not prove retention. |
| Recovery | Attach compatible packet/identity comparisons, exact failed/successful execution records and cumulative charges around the explicit model switch. |
| Usability | Build/serve on loopback, open index/search, navigate chapter/concept/source links and representative figures, check rendered math and mobile-width readability; record defects. |

Compare with a local baseline only if provided, otherwise say unavailable. Historical
runs with different model, effort, input shape or route are descriptive only.
An optional small stock/coverage experiment must use fresh isolated KB/run roots
with identical source units, model, effort, route and budget, changing only policy.
Do not attribute model/prompt/input-size differences to OAuth versus API.

The human report may recommend READY_FOR_REVIEW only with evidence for all required
checks and named limitations; this is not semantic completeness or merge approval.
Otherwise keep NOT_READY with concrete blocking findings. Full-corpus launch needs
a separate explicit decision and budget after this report is reviewed.

Remaining inputs for the target-book run: private book and optional baseline paths,
source-backed evaluation and visual sampling decisions, established account route,
selected primary/recovery models and effort, and explicit cumulative/per-call budgets.
No live usage, private wiki, browser usability result or semantic readiness is claimed
by this implementation.
