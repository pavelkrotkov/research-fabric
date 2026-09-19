# Complete native compilation and run finalization

`research_fabric.compilation.compile_with_recovery` is the workflow's compilation
boundary. Its default child invokes native OpenKB, in a disposable byte-copy of
the run KB. The same boundary is used by credential-free offline tests. The
publication-only rehearsal remains publication-only.

## Qualified environment

Use Python 3.12+ and install the engine plus `tests/native-requirements.txt`:

```sh
uv venv --python 3.12
uv pip install -e . pytest -r tests/native-requirements.txt
.venv/bin/python -m pytest tests/test_native_compilation.py tests/test_compilation.py
```

The workflow interpreter must have this environment. OpenKB **0.4.5** and
LiteLLM **1.87.2** are qualified. The adapter also checks the compiler module's
SHA-256 against the qualified release. A version/source mismatch fails before
mutation; run and review the native tests before changing the pins or adapter.
The workflow integration fixture reuses a validated synthetic evidence packet;
it does not claim to test live extraction. It substitutes only CAO transport,
external provider responses and advisory semantic lint.
No credentials, live providers, private corpus, or CAO installation are required
by these tests. The optional native CI job executes them on Python 3.12.

## Completion evidence

The private `_openkb045` adapter observes the final normalized native plan at
its index-update boundary, after required page generation, writes and backlinks.
It verifies exact required page-writer counts, actual bytes at native atomic
writes, summary application and reciprocal links. Missing outcomes, silent
no-op writes, unknown completion paths and swallowed required-page errors fail
inside the native add mutation, activating OpenKB's rollback machinery. Native
page formatting, planning, normalization, index updates and transactions remain
native implementations. Optional summary rewrite failure retains the native
fallback and is recorded separately.

Instrumentation exists only in the executable child and is restored on exit.
The module has no tracing hooks, alternate compiler or request-count heuristic.
Directory inputs are passed as explicit single-file operations; any failed file
fails the whole candidate, despite native directory CLI's exit-zero behavior.

Native retries run inside one mutation without restoring partial writes. The
adapter disables those inner retries. The outer seam instead allows two attempts
(default; configurable 1–3), each in a fresh copy of the same baseline. Each child
has a 30-minute process deadline. Inputs, baseline bytes, Git revision, config,
environment fingerprint, adapter and executable identities are checked again
before each attempt and before applying successful output.

All supplied sources are conservatively recompiled, including unchanged ones.
Native hash entries are invalidated only in the isolated candidate; a registry
hit is never an acceptance record. This deliberately avoids a second source
registry or a stale-acceptance cache. Existing registry entries for other sources
remain native-owned. No successful provider response alone proves a page write.

Outstanding native journals are rejected before copying or accepting a candidate:
they contain absolute recovery paths. Recover them in their original KB with
native OpenKB before creating a fresh run; never transplant a recovery journal.

## Receipts, failures and recovery

Diagnostics live under `run/verification/compile/compile-*/`: `identity.json`,
`baseline/`, `candidate-N/`, `result-N.json`, `failure-N.json`, and on completion
`completed.json`. The completion receipt binds the **compile-stage** output bytes;
it is not `READY_FOR_REVIEW` and not a final-publication tree attestation. Native
lint and evidence materialization follow. Native logs that could contain provider
bodies are suppressed by the production child; structured outcome/error codes
remain. Candidates/baselines contain private KB data and must remain outside Git.
The session directory is private (created with `mkdtemp`'s restricted permissions).

A failed attempt cannot modify the original candidate. A crash during copying
completed output can leave a dirty run branch, but never a ready run. Do not
retry on that dirty branch or remove only a source hash. Preserve its diagnostics
and create a fresh isolated worktree from `identity.json`'s `base_commit`:

```sh
git worktree add -b agent/retry-RUN /path/to/fresh-worktree BASE_COMMIT
```

Run the workflow with that fresh `field_root`, the same immutable source inputs
and manifest, and (optionally) the saved evidence directory. Use a new run root
and retain the failed run root. This reruns all deterministic gates and validates
current policy/toolchain; no old completion receipt is reused. If input or policy
changed, treat it as a new run. Never restore a failed candidate over main or a
worktree containing unrelated user edits.

The workflow clears stale success at entry, requires an initially clean non-main
branch, and records escaping exceptions as `FAILED` through `run_lifecycle`.
A hard crash leaves a non-ready state. It records `READY_FOR_REVIEW` atomically
only after provenance collection, staged whitespace validation, proposed commit
and clean-tree verification. Required compiled pages must retain their attested
bytes through later phases. All wiki/evidence outputs must be tracked (ignored
publication files fail rather than being force-added), and their actual proposed
commit blobs must match the final gated bytes, even if a Git hook rewrites them.
Private native state is excluded from that publication-file check. Native-generated log EOF padding is normalized
before compilation hashing and after lint, before deterministic gates and diff
review; immutable snapshots and page bodies are untouched. Human merge is still
required. Later compile-output audit/publication consolidation belongs to #13/#18.
