# ADR-001: Deterministic gates authoritative, LLM advisory-only

## Status
Accepted (2026-08-25, full-Odyssey run)

## Context
The pipeline's job is to publish claim-level evidence into a knowledge base.
The LLM verifier (Claude in a read-only CAO terminal) was originally a hard
gate. Across runs it emitted: a bare `FAIL` followed by "Verified clean … all
37 locators resolve"; a bare "Findings"; "Verified sound"; and a FAIL with an
empty defect list. None of these are parseable into a trustworthy machine
verdict without loosening the contract until it becomes rubber-stamping.

Meanwhile the checks the verifier was *supposed* to perform (excerpt occurs in
cited snapshot; source id exists in the source ledger) are **mechanically
decidable** from the bytes on disk.

## Decision
Only deterministic, byte-level gates decide publication:

1. **Provenance** — every snapshot's sha256 matches its manifest row; the
   claims ledger and source ledger cross-reference.
2. **Excerpt grounding** — every claim's `excerpt` is a verbatim substring of
   the snapshot named by its `source_ids` (normalization tolerates only
   HTML-entity, punctuation-folding, and whitespace-reflow artifacts).

The LLM verifier still runs at two points (pre-ingest, post-compile) and its
full output is captured under `verification/` for human review — but its
verdict is recorded as **advisory** and never blocks or unblocks publication.
A negative control suite (`bin/negative_controls.py`) exists so the
deterministic gates themselves are tested against known-bad inputs.

## Consequences
- A fabricated or drifted quotation fails closed regardless of agent
  judgement — this is what caught 21 ungrounded claims in the full-Odyssey run
  before they reached the knowledge base.
- Verifier flakiness (tmux cold starts, quota, malformed verdicts) can no
  longer wedge the pipeline.
- The trade: the pipeline cannot catch *semantically* false-but-verbatim
  claims (the source really says it, but the claim misinterprets it). That
  residual is exactly what human review at merge time covers.