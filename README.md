# research-fabric

> **Public engine.** This repo contains the deterministic, fail-closed research-evidence
> pipeline and its gates. The **immutable corpora** (source snapshots + sha256 manifests) are
> not published here — they live in the private `research-fabric-corpora` repo, because some
> corpora (e.g. the Lit & Hist podcast **transcripts**) are under copyright. Point the
> engine at a corpus with `RESEARCH_FABRIC_ROOT` / `RESEARCH_FABRIC_PROJECTS` /
> `RESEARCH_FABRIC_CORPUS`, or run it with a corpus you supply.

A reproducible research pipeline that turns immutable source snapshots into a
verified, claim-level evidence corpus inside an [OpenKB](https://github.com)
knowledge base. Built for and proven on the full *Odyssey* (24 books, 363
claims), it enforces a hard rule: **nothing is published that the byte-level
verifier cannot re-verify against the source.**

## Scope of this repo

- **Engine** — `workflows/research.py`, `src/research_fabric/core.py`, the gates in `bin/`,
  `tools/wiki/`, project specs in `projects/`.
- **Not here** — `corpora/`, `runs/`, `worktrees/` (deployment data, in `research-fabric-corpora`),
  and the host-bound harnesses that need a specific corpus (`bin/negative_controls.py`,
  `bin/collect_missing.sh`).
- The controller-box host paths (e.g. `/home/pavel/research-fabric`) are **defaults**; override
  them via the `RESEARCH_FABRIC_*` env vars so the same engine runs anywhere.

## What a run does

```
source snapshots (immutable, sha256-manifested)
        │
        ▼
  plan (bounded, non-overlapping per-book assignments)
        │
        ▼
  collect ─── direct-API workers (one LLM call per book, strict JSON packet)
        │        retries, structural packet validation (fail-closed)
        ▼
  advisory LLM verifier (recorded, never gates)
        │
        ▼
  deterministic gates (all fail-closed, all byte-exact):
    • provenance: every snapshot matches its manifest sha256
    • excerpt grounding: every claim excerpt is a verbatim substring of its snapshot
        │
        ▼
  compile (openkb add + lint) → claims ledger (evidence/claims.jsonl)
        │
        ▼
  commit on an isolated worktree branch → READY_FOR_REVIEW
        │
        ▼
  human review → explicit merge to main → rebuild hosted wiki
```

Every run carries a single deterministic terminal state in `run.json`:
`PLANNING → RESEARCHING → VERIFYING_SOURCES → COMPILING →
MATERIALIZING_LEDGER → VERIFYING_DIFF → READY_FOR_REVIEW` (or `FAILED` with a
reason at any point).

## Layout

```
workflows/research.py      the full pipeline (CAO workflow script)
bin/direct_worker.py       evidence worker: HTML → text → one ox-alpha call → validated packet
bin/repair_claims.py       claim-level repair: re-ground only failed claims against the source
bin/excerpt_grounding.py   gate: every excerpt must be a verbatim substring of its snapshot
bin/provenance_validate.py gate: claims ledger ↔ source ledger ↔ snapshot bytes
bin/negative_controls.py   gate test-suite: known-bad packets must be rejected
bin/collect_missing.sh     batch driver for missing-book collection
bin/dryrun_publication.py  offline publication rehearsal
profiles/                  CAO agent profiles (supervisor, verifier — advisory roles)
projects/                  per-corpus project specs (projects/odyssey.yaml)
corpora/                   immutable corpora: one dir per project (sources + sha256 manifest)
tools/wiki/                generic OpenKB-vault → MkDocs static-wiki builder
fields.yaml                KB field registry (classics, energy-storage, …)
runs/                      per-run state, evidence packets, verification artifacts
worktrees/                 per-run isolated git worktrees (one branch per run)
archive/                   superseded iterations (diagnostics, smoke tests, early worker variants)
docs/adr/                  architecture decision records
```

## Reproducing the full-Odyssey result

```bash
# on the controller box (see below for layout)
cao workflow run research \
  --run-id <stamp>-odyssey-full \
  --input run_root=$RUN \
  --input source_dir=$RUN/sources \
  --input field_root=$WORKTREE \
  --input question='The full Odyssey (Books 1-24): claim-level evidence across the entire epic'
```

`source_dir` must contain snapshots matching the project's `snapshot_pattern`
and carry a `source-manifest.jsonl` (sha256 per snapshot) in the run root.
The canonical manifest, derived from the project spec, lives at
`corpora/<project>/source-manifest.jsonl` and is re-verified at publish.

## Reproducibility chain

Every successful run writes a `provenance` block into the run's `run.json`
recording the exact toolchain + inputs, so any published KB commit is
re-auditable without the original session:

```text
KB commit (message includes run-id)
    └─ run-id
         └─ engine Git SHA + tag
         └─ project name + project-spec SHA
         └─ corpus manifest SHA + corpus sources content-hash
         └─ model, provider, openkb/CAO/python versions
```

The KB commit message ends with `run <run-id>` (e.g. "…Books 1-24; run
20260825T154543Z-odyssey-full-publication7"), closing the loop from a claim in
the KB back to the exact code, spec, corpus bytes, and model that produced it.

## Host requirements (abstracted)

The full pipeline needs an installed environment. Exact controller paths and
config are documented in `research-fabric-corpora` (private); here are the
portable requirements:

- A Python venv with the `openai` SDK (the direct-API worker — see ADR-0003).
- `openkb` (pinned 0.4.5) and `cao` (CAO 2.4.1 + lifecycle patches), plus a
  local CAO server, for the orchestration layer.
- An OpenRouter-capable API key for the worker model (set via the
  `OPENROUTER_API_KEY` environment variable; the worker falls back to
  reading it from the host's Hermes env file if unset).
- 2-core host: collection is deliberately serialized (`max_workers=1`).

## Architecture decisions

See [`docs/adr/`](docs/adr/):

1. Deterministic gates authoritative, LLM advisory-only
2. Fail-closed publication with one terminal status
3. Direct-API workers, not CLI-agent-in-tmux
4. Immutable source manifests as the provenance root
5. Isolation + human merge (worktrees; main is never touched by a run)
6. Claim-level, not book-level, repair

## Comparison: research-fabric vs. the `llm-wiki` skill

Both produce persistent, compounding markdown knowledge bases. They are built
on **opposite trust models** and are complementary: the wiki OpenKB compiles
from this pipeline's claims *is* llm-wiki-shaped output, just produced under
gates.

| Dimension | `llm-wiki` skill (Karpathy pattern) | research-fabric |
|---|---|---|
| **Compiler** | The agent, in-context, following a schema | Deterministic scripted pipeline; LLM only proposes claims |
| **Unit of knowledge** | The *page* (entity / concept / comparison) | The *claim* (atomic: locator + verbatim excerpt + stance + confidence) |
| **Provenance** | Soft — `sources:` frontmatter, `^[raw/…]` markers, raw sha256 drift check | Hard — every excerpt must be a **verbatim substring** of the immutable snapshot, machine-checked |
| **Quality gate** | None — lint is advisory, surfaces `contested:` / `confidence: low` for humans | Fail-closed — grounding + provenance gates block publication outright |
| **Contradictions** | Note both positions, mark `contested: true`, human resolves | Structured `conflicts` array in the packet, surfaced in the ledger |
| **Synthesis** | Agent writes interlinked `[[wikilink]]` pages (2+ outbound links, 200-line split) | OpenKB compiles pages from the verified claim ledger; synthesis downstream of verification |
| **Reproducibility** | Re-ingesting the same source may yield different pages | Same inputs → same deterministic gates; generated claims may differ, but accepted claims must satisfy identical grounding/provenance rules |
| **Failure semantics** | None — agent judgment + human review | One deterministic terminal status per run |
| **Scale model** | One agent session, personal KB (index nav rules for ~100–200 pages) | Batch runs, per-book workers, valid-packet carry-forward, per-claim repair loop |
| **Trust statement** | *"The agent compiles; the human curates."* | *"The agent proposes; the byte-level verifier disposes."* |

**When to use which.** llm-wiki is right for fast-moving domains where nuance
matters more than certainty and the human stays in the loop on every ingest.
research-fabric is right for fixed, authoritative corpora (primary texts,
specs, legal documents) where a single hallucinated quote is poison and the
corpus must be re-verifiable years later without the original session.
