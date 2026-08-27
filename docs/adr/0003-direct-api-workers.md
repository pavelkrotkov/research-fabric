# ADR-003: Direct-API workers, not CLI-agent-in-tmux

## Status
Accepted (2026-08-25, full-Odyssey run)

## Context
Evidence collection originally ran through CAO: each worker was a CLI agent
(Codex, then Hermes) launched in a tmux pane. Two independent problems made
this path unusable for the full corpus:

1. **Quota coupling.** The Codex-backed workers died when the account hit its
   usage cap (a "1 usage limit reset available" banner in the pane), stalling
   a 24-book run for days. Provider choice was entangled with the host's
   subscription state.
2. **Structural truncation.** CAO's Hermes provider recovers the agent's
   reply by scraping the terminal scrollback with a **200-line capture
   window**. A large evidence packet (10–16 claims, ~11 KB of JSON) does not
   fit; the tail — often the JSON closing braces — is cut, so `extract_json`
   fails on otherwise-fine output. No prompt or retry fixes a bounded
   capture window.

## Decision
Evidence collection bypasses the agent-in-tmux path entirely. A small host
script (`bin/direct_worker.py`):

- reads the book's HTML snapshot from disk and strips it to text,
- makes **one** chat-completions call to `stealth/ox-alpha` via OpenRouter
  (openai SDK, `temperature=0`, bounded 429/5xx backoff),
- parses the JSON packet from the **response body** — there is no screen to
  scrape, so nothing is truncated,
- writes a structurally validated packet to the run's evidence dir.

The workflow (`workflows/research.py`) still *orchestrates* everything else —
ingest, snapshot verification, the deterministic gates, compile, commit — and
still re-validates each packet with the fail-closed `packet_defects()` check.
CAO remains the run orchestrator; only the extraction step is a direct API
call.

## Consequences
- Extraction is **quota-independent** and **complete by construction**: the
  full JSON is in the API response, not in a terminal buffer.
- No Codex/Hermes CLI on the collection hot path, so no tmux cold-starts, no
  shell-init gate timeouts on the 2-core host, no scrollback corruption.
- Trade: we own the HTTP/backoff/retry logic ourselves (~135 LOC) instead of
  relying on a provider framework. This is small, tested, and the right place
  for it.
- The LLM is still a *proposer* of claims; nothing about verification changed
  (see ADR-001). This decision is only about *how the proposal is obtained*.