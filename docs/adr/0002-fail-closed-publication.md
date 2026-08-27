# ADR-002: Fail-closed publication with one deterministic terminal status

## Status
Accepted (2026-08-13, pilot; reaffirmed 2026-08-25)

## Context
A research pipeline that *can* publish a partial or unverified result is worse
than one that cannot. Early prototypes had two failure shapes that were both
dangerous: (a) a structurally valid packet that was evidentially empty (a
worker that never read its sources returned `{"claims": []}` plus an
apologetic note, and passed the JSON check), and (b) a "success" that left the
knowledge-base worktree dirty, so the next run started from unexplained local
changes.

## Decision
Publication is **fail-closed**. Every run ends in exactly one terminal state,
written to `run.json`:

- `READY_FOR_REVIEW` — all gates passed, a clean commit exists on the run
  branch, worktree verified empty via `git status --porcelain`.
- `FAILED` with a `failure` reason — at the *first* gate that fails. No
  partial commit, no "published-but-warning" state.

Supporting rules:
- **Packet validation is evidential, not just structural.** `packet_defects()`
  rejects zero-claim packets, placeholder schema echoes (literal `"..."`
  values), and non-concrete stance values. This was added after a full-Odyssey
  run produced a book-1 packet of all-`"..."` placeholders that passed the
  structural check.
- **No commit unless the tree ends clean.** After the commit the script
  re-runs `git status --porcelain`; any residual dirt fails the run.
- **A generated diff must be non-empty.** `git add -N` registers new evidence
  files so the diff attests the real change set; an empty diff is a hard
  failure (refusing to attest nothing).

## Consequences
- The pipeline can never silently publish garbage or leave the KB in an
  ambiguous state. The cost is that a single bad claim fails the whole run —
  which is why ADR-006 (claim-level repair) exists to recover cheaply instead
  of re-extracting entire books.
- "Done" has a single, checkable meaning: `run.json` says `READY_FOR_REVIEW`
  *and* the worktree is clean. A human or script can verify completion without
  reading any prose.
