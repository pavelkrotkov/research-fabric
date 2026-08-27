# MVP `mvp-odyssey-0.1` — public build record

This is the **sanitized** record of the first valid full-Odyssey run. The detailed
forensic record (exact controller host paths, full provenance chain, per-tool
config) lives in the private `research-fabric-corpora` repo; this page keeps only
what is useful and safe to publish.

## Outcome

- **Run:** `20260825T154543Z-odyssey-full-publication7`
- **Result:** 24/24 books, 363 claims, grounding + provenance gates green, published
  to the knowledge base and a hosted MkDocs wiki.
- **Engine commits:** the deterministic engine tagged `mvp-odyssey-0.1` and this
  repo's `main` share the same byte-identical `workflows/research.py`; see
  `docs/adr/` for the architecture (fail-closed deterministic gates authoritative).

## Reproduction contract

A run reads an **immutable corpus**: each source snapshot is pinned by a sha256
manifest (byte-exact). The engine:

1. Copies the selected snapshots into the run's evidence dir.
2. Runs the claim-collection step (an LLM worker — see ADR-0003: direct-API workers).
3. **Gates, which are deterministic and authoritative:**
   - `excerpt_grounding.py` — every cited excerpt must be a verbatim (elision-aware)
     substring of its source snapshot.
   - `provenance_validate.py` — every manifest row re-hashes byte-exact, every claim
     references a real source_id, confidence in [0,1].
4. Only if all gates pass does the run reach `READY_FOR_REVIEW` and commit a ledger.

**Reproducibility scope:** same inputs → same *deterministic gates and audit trail*.
Generated claims may differ between runs (the LLM worker is not byte-deterministic);
what is guaranteed is that any accepted claim must satisfy the same grounding and
provenance rules, and the full ledger is re-verifiable from the manifest without the
original generation environment.

## Dependency lock (abstracted)

- **OpenKB** 0.4.5 (pinned) — KB engine.
- **CAO** (`cli-agent-orchestrator`) 2.4.1 + a small fork — run orchestration.
- **Worker model** `stealth/ox-alpha` over the OpenRouter API (direct-API worker).
- **Python** ≥3.10; pinned exact versions + `uv.lock` are in-tree.

<!--- The authoritative release of the exact tool versions and controller-path
      configuration is in research-fabric-corpora (tag mvp-odyssey-0.1). --->
