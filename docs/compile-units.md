# Bounded native compilation

Reading projects compile one frozen reading per native OpenKB input, in order,
into one shared wiki. Legacy HTML/witness projects still use the existing whole
snapshot compilation path. OpenKB 0.4.5 remains the planner, normalizer, writer,
linker and native registry owner; no replacement wiki writer is involved.

## Configuration and pre-call planning

```yaml
compilation:
  mode: coverage                 # coverage (default) or stock comparison
  max_request_tokens: 32000       # explicitly qualify against your selected model
execution:
  budget:
    requests: 100                 # choose explicitly; never auto-expanded
    output_tokens: 20000
    attempt_tokens: 100000
```

`reading.policy.target_tokens`, `soft_limit_tokens` and `context_budget_tokens`
remain segmentation hypotheses, not model capacity claims. Before extraction,
`compile-planning-report.json` records unit input and scoped citation/policy BPE
sizes, unresolved units, independent Works versus native notes, configured quota,
and minimum fresh request demand: one extraction plus three native calls per
reading. Concept/entity writes and retries add demand. External advisory and native
lint are not covered by that minimum (nor by the shared model-transport quota).

Admission is intentionally conservative: UTF-8 payload bytes, framing allowance
and output reserve must fit the smaller of `max_request_tokens` and
`execution.budget.attempt_tokens`. Byte counts are an upper bound for the pinned
byte-level text encoding, not provider-specific billing. A model/provider with a
different envelope needs explicit qualification; the default is not a claim that
all models support this context size. At the actual native transport boundary,
all messages (schema, source, context, inline figures, summaries, existing pages,
whitelists and scoped citation mappings) plus output reserve are checked again,
before reservation or transport. Remote image URLs are rejected as unbounded.
An optional native-summary fallback cannot hide an envelope rejection.

Static observations cannot predict future page size or native operation count.
Passing preflight is not a promise that the run will fit. Dynamic overflow stops
without truncating source/context, and consumed calls remain charged. To reduce
oversized coherent readings, author a syntax-safe `reading.reviewed_plan` with
explicit smaller section ranges/readings or reasoned context/excluded dispositions,
then start a new run. Existing reading validation rejects cuts through paragraphs,
math, tables, fences and reference definitions. There is no automatic arbitrary
character slicing. Missing context, changed policy and incompatible plans fail closed.

## Identity, isolation and resume

`compile-plan.json` freezes each unit's reading ID, parent Work, primary/context
section mappings, original byte/line coordinates, source/representation identity,
asset closure, explicit derived-context list, native policy and input digest. The
stable unit ID is also its native input filename and summary name. Whole original
snapshots are archived once, not supplied in every compile request. Prepared unit
headers are labels, never original citation coordinates. Two sections of one Work
remain one independent support group; native document counts are only note counts.

Only the unit's primary/context citations enter the transient supported per-KB
`wiki/AGENTS.md`. The original policy is restored after each attempt. The complete
reading/compile mapping remains durable outside prompts and is published under
`evidence/`. Each unit uses the existing isolated accepted-candidate recovery seam,
including native rollback and required-operation outcomes. Native parent hashes
cannot skip units: unit content has its own hash, and native registry dedup is not
accepted as proof of completed compilation.

`verification/compile/units/checkpoint.json` attests the private candidate and its
accepted prefix. A failure leaves the real KB unchanged. With compatible inputs,
resume reuses that prefix, not failed native state; original execution provenance
is retained. Source, reading/compile plan, policy, native toolchain, command or
candidate drift invalidates reuse. Operator budget/profile changes remain explicit
execution revisions and do not replenish prior spend. Interrupted candidate
application is not recoverable by trusting partial public bytes: use a fresh clean
worktree/run. The final aggregate `completed.json` is emitted only after all units
and asset publication succeed. Shared publication and offline rehearsal consume that
same receipt and unit-to-note mapping, not reconstructed source basenames.

## Coverage and retention policy

Coverage mode uses OpenKB's supported per-KB policy. It asks for important concepts,
mathematical assumptions, qualifications, uncertainty and conflicts without a
universal page-count target, and explicitly rejects using the stock initial
2–3-concept guideline as a coverage ceiling. The qualified compiler itself is not
patched to invent operations. `mode: stock` omits this coverage policy while
retaining bounded inputs and citation provenance for comparison.

`evidence/coverage.json` distinguishes:

- Mechanical completeness: each primary section has one accepted compile outcome;
  other sections retain explicit context/excluded dispositions and reasons.
- Advisory evidence: citing pages per original section, changed existing pages,
  before/after hashes and actual per-unit diffs. Removed distinctions are visible
  even when a model forgets them. Private native attempt baselines are also retained.
- Semantic completeness: always unknown, with human review required. Neither page
  count, citation presence nor an LLM opinion passes this part automatically.

`evidence/unit-outcomes.json` maps every native summary/required operation to the
frozen unit/Work/spans/assets and the recorded unit execution attempts. Existing
claim provenance and grounding gates still apply. Changes to earlier supported
material must be reviewed; a policy prompt is not a mathematical retention guarantee.

## Reproducible public one-book experiment

In the qualified native environment from `tests/native-requirements.txt`, provision
`cl100k_base` data as described in `reading-plans.md`, then run:

```sh
python -m pytest -vv tests/test_native_compilation.py -k source_mapped
python -m pytest -vv tests/test_compile_units.py
```

The first test creates a synthetic multi-section book chapter plus two papers,
with nested Unicode names, repeated headings, CRLF, math, fenced content and a
figure. It exercises actual worker dispatch, native planner/writer, failure after
an accepted prefix, explicit budget increase/resume, common publication gates,
clean proposed commit, browser export and the offline rehearsal. Only external
model responses are replayed. Captured requests prove source/citation scoping;
source bytes/coordinates, unit receipts, Work counts and retention evidence are
asserted. The stock native tests retain the two-concept response fixture; the
coverage fixture supplies four operations and later qualifications/contradictions.
This verifies native policy plumbing and handling of those outcomes, not that a
live model will obey or that four pages imply semantic coverage.

For a live one-book comparison, use two fresh isolated KB/run directories with the
same frozen reading assignment and explicit budget, changing only `mode: stock`
to `mode: coverage`. Compare section dispositions, missing citations, operation
receipts and retention diffs against the original text. Record actual requests and
human findings; do not infer improvement from page count. No private corpus is
required by the regression tests.

## Visual boundary

Units include existing verified source-figure closures and literal captions.
Derived-context references are explicitly empty in this version: it does not
mislabel generated visual descriptions as original evidence. Full visual-book
coverage still requires the separately tracked oversized-raster/derived-visual
integration (#37/#38). Unsupported/oversized visual envelopes fail, rather than
claiming full coverage or silently dropping a required image.
