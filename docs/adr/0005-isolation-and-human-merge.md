# ADR-005: Isolation + human merge (worktrees; main is never touched by a run)

## Status
Accepted (2026-08-09, pilot)

## Context
The knowledge base has a long-lived `main` branch that humans read and that a
hosted wiki is rebuilt from. An automated run that writes directly to `main`
could: land a half-verified corpus, leave the branch mid-run when it fails,
or interleave with a human edit. Even with fail-closed gates, the *merge* is
where trust changes hands, and that should be a human act.

## Decision
- Each run executes in an **isolated git worktree** on its own branch
  (`agent/<run-id>`). The run's compile, ledger, and commit all happen there.
- **No workflow step ever writes to `main`.** Mutation-capable operations live
  in host-side scripts, never inside research agents (agents are read-only).
- The run ends at `READY_FOR_REVIEW` — a *proposed* commit on the run
  branch. A human reviews the wiki and the claim ledger, then explicitly
  merges (`git merge --no-ff`) into `main`, keeping the run's history visible.
- After merge, the run branch and worktree are dropped.

## Consequences
- A failed run cannot corrupt the corpus or the hosted wiki; worst case it
  leaves a dead branch.
- Merge conflicts (e.g. two runs touching `wiki/log.md`) are resolved by a
  human at merge time with full context, not by an agent mid-pipeline.
- Trade: the human is a required step. This is deliberate — the pipeline is
  designed for a small number of high-stakes publications, not firehose
  ingestion.
