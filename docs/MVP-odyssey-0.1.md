# MVP `mvp-odyssey-0.1` — reproducible build record

Tag **`mvp-odyssey-0.1`** freezes the exact code + tools + inputs that
produced the first valid full-Odyssey knowledge base. `git checkout
mvp-odyssey-0.1` reproduces the system that ingested the epic — this is the
debugging anchor if any later refactor behaves differently.

## What this MVP produced

| Artifact | Value |
|---|---|
| Knowledge base | `zettelkasten/classics` (`main`) |
| KB merge commit | `4f631fc` ("Merge full-Odyssey evidence run") |
| Run commit (branch) | `b140132` (363 claims) |
| Run id | `20260825T154543Z-odyssey-full-publication7` |
| Claims published | 363 (Books 1–24), `evidence/claims.jsonl` |
| Corpus | 24 Theoi html snapshots + `source-manifest.jsonl` (sha256) |
| Hosted wiki | `https://controller.tail377b2a.ts.net/wiki/` (instance) |

## Provenance chain (this MVP)

```
KB commit 4f631fc
    └─ run-id 20260825T154543Z-odyssey-full-publication7
         └─ engine Git SHA (this tag / 8a82488)
         └─ project spec: in-engine (Odyssey baked into research.py) — MVP only
         └─ corpus manifest SHA256 (sources/odyssey-full.source-manifest.jsonl)
         └─ snapshot SHA256s (24 rows, re-verified at publish)
         └─ tool versions below
```

> Note: at this MVP there is no separate `projects/*.yaml` spec — the Odyssey
> behavior is hardcoded in `workflows/research.py`. The generalization
> (extracting `projects/odyssey.yaml`) is the *next* change after this tag, by
> design, so this tag is bitexact with what actually ran. After that
> generalization, the corpus lives at `corpora/odyssey/` (not `sources/`); the
> tag's `sources/` paths are the historical-accurate ones.

## Tool / dependency lock

| Component | Version | Notes |
|---|---|---|
| `openkb` | **0.4.5** | pinned (strict); CLI at `/home/pavel/.local/bin/openkb` |
| CAO (`cli-agent-orchestrator`) | **2.4.1 + fork `852502a`** | fork `pavelkrotkov/cli-agent-orchestrator` main = `852502a`; `server.provider_init_timeout=300` in `~/.aws/cli-agent-orchestrator/settings.json` |
| `openai` SDK | 2.24.0 | venv `~/.hermes/hermes-agent/venv` |
| python | 3.11.15 | same venv |
| model | `stealth/ox-alpha` | via OpenRouter `https://openrouter.ai/api/v1` |
| key | `OPENROUTER_API_KEY` | in `~/.hermes/.env` |
| Hermes | 0.20.0 / v2026.8.3 | runtime |
| platform | Debian/controller, x86_64 | Core 2 Duo P8600 (no AVX/BMI — constrains numpy/pyarrow) |

## How the worker ran (MVP path)

`workflows/research.py` orchestrates via CAO. Evidence collection is a
**direct-API** call (`bin/direct_worker.py`): read HTML → strip to text →
one `stealth/ox-alpha` chat completion (`temperature=0`, 20k `max_tokens`,
bounded 429/5xx backoff) → parse JSON from the response body → write a
structurally-validated packet. **No CAO tmux agent on the collection path**
(ADR-003) — deterministic gates in `bin/` remain authoritative (ADR-001).

## What is NOT in this tag

- Transient run state (`runs/`, `worktrees/`) — gitignored
- `archive/` superseded iterations — gitignored (kept on disk)
- venvs, FIFOs, logs, secrets — never tracked
- Generated wiki site (142M) — instance lives in `~/services/odyssey-wiki`

## Verifying this is what ran

```bash
sha256sum workflows/research.py \
  /home/pavel/.aws/cli-agent-orchestrator/workflows/research.py
# both = 70f483f6f24f863b6f1af93da1e5da827275aec6484c241f25e18d5550134815
```
