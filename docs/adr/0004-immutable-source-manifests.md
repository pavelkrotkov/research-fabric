# ADR-004: Immutable source manifests as the provenance root

## Status
Accepted (2026-08-13, pilot)

## Context
An evidence corpus is only as trustworthy as the sources it cites. Two threats
matter: (a) a claim could cite a snapshot that was silently edited after
collection, and (b) a re-run could be pointed at a subtly different source
byte sequence and produce claims that no longer correspond to what was
originally reviewed. Both are invisible without a hash-of-record.

## Decision
Every run carries (or falls back to a canonical) **`source-manifest.jsonl`** —
one row per source snapshot with a `sha256` digest of the exact bytes and a
stable `source_id`. The provenance gate (`bin/provenance_validate.py`) runs
*before any agent judgement* and fails closed if:

- any snapshot's recomputed sha256 differs from its manifest row,
- the source manifest is missing an entry for a present snapshot, or
- the claims ledger references a `source_id` absent from the source ledger.

Snapshots are copied into the knowledge base and made **read-only
(`chmod 0o444`)** so the published corpus and the verified bytes cannot
diverge.

## Consequences
- **Reproducible by construction.** Same manifest → same bytes → same
  grounding result, on any run. The full-Odyssey result is re-verifiable
  without the original session: the manifest is the anchor.
- A drifted or hand-edited source is a *hard failure*, not a silent
  contradiction.
- Trade: the manifest must be produced from the exact bytes that are
  collected, so the fetch step and the manifest step are coupled. In practice
  the 24-book fetch verified each snapshot's digest against the manifest
  immediately after download (3× byte-identical for the carried-over pilot
  books).
