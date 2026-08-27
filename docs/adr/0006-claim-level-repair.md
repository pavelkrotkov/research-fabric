# ADR-006: Claim-level, not book-level, repair

## Status
Accepted (2026-08-25, full-Odyssey run)

## Context
The deterministic grounding gate is strict: in the full-Odyssey run it
correctly rejected 21 of 374 claims whose excerpts were not verbatim in the
cited snapshot (the model had paraphrased or reflowed them). With
fail-closed semantics (ADR-002), those 21 failures sink the *whole* run — even
though 353 claims are fine. The obvious recovery, re-extracting the affected
books, is wasteful: 21 bad claims were scattered across only 7 books, and
re-running a whole book spends a large generation to fix a handful of excerpts
while risking new drift in claims that were already good.

## Decision
Repair at the **claim** level, not the book level. `bin/repair_claims.py`:

1. parses the grounding report for the specific failed claim ids,
2. for each, re-prompts the model with just that claim's text + its rejected
   excerpt + the book's text: *"find the exact verbatim span this excerpt
   came from, or say `found: false`,"*
3. if a verbatim span is returned, **replaces the excerpt in place** (the
   claim text and stance are preserved);
4. if the model cannot ground it (`found: false` or the new excerpt still
   isn't a substring), the claim is **dropped**.

The repair writes a marker into the packet's attempt log. The full
deterministic gate then re-runs on the whole corpus and has final say — a
repaired excerpt only publishes if the grounding gate accepts it.

## Consequences
- Cheaper and safer recovery: ~21 targeted calls instead of 7 full-book
  re-extractions, and no already-good claim is at risk of drift.
- Combined with ADR-002, the pipeline's failure mode becomes **cheap and
  reversible**: a grounding failure is a repair-and-re-gate loop, not a
  start-over.
- In the actual run, pass 1 repaired 6 / dropped 9, pass 2 repaired 7 /
  dropped 1, and the final corpus landed at **363 claims, 0 ungrounded** —
  with the grounding gate as the arbiter throughout.
- Trade: the claim *text* is kept even when its excerpt changes, so the
  claim's meaning is re-validated by the human at review rather than by
  re-derivation. The grounding gate still guarantees the *evidence* is
  verbatim.
