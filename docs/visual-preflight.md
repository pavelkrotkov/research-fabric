# Optional native OAuth visual preflight

Text collection and compilation need no OAuth. This optional preflight uses OpenKB's
read-only `build_query_agent`, Agents `Runner`, and native `get_image` to inspect
explicitly supplied PNG/JPEG/WebP files in a disposable wiki. It cannot write the
original Markdown or canonical wiki. The direct evidence-worker transport remains
unchanged (ADR-003), and generated notes remain advisory (ADR-001).

Install an isolated optional environment, with research-fabric importable:

```sh
uv venv /tmp/visual-env
uv pip install --python /tmp/visual-env/bin/python -r tests/visual-requirements.txt -e . pytest
```

Supply a JSON spec, using the exact image SHA-256 and original-asset locator:

```json
{
  "model": "chatgpt/gpt-6-astra",
  "effort": "medium",
  "instructions": "Transcribe equations carefully; explicitly report uncertain symbols.",
  "assets": [
    {"path": "images/equation.png", "sha256": "<64-character digest>", "source_locator": "chapter.md#equation-1"}
  ]
}
```

Run with the user's existing LiteLLM ChatGPT OAuth account (`CHATGPT_TOKEN_DIR` and
`CHATGPT_AUTH_FILE` retain their native meanings). There is **no API-key fallback**,
provider switch, model fallback, or adapter retry. A missing account may trigger the
native OAuth login flow; establish the account before unattended runs.

```sh
/tmp/visual-env/bin/python bin/visual_preflight.py \
  --source-root /path/to/sources --spec /path/to/visual.json \
  --output /path/to/run/verification/visual-inspection.json
```

For the research workflow, pass `visual_preflight_spec=/path/to/visual.json` and
`visual_python=/tmp/visual-env/bin/python`. Legacy projects append a labeled note to
supervisor preparation. Markdown reading projects instead inspect each selected raster
separately and route its note using the frozen reading's primary/context asset closure.
Matching uses the full source-root-relative original path and original/derivative hashes,
never basename or a run-wide note dump. Shared figures reach all assigned readers;
unassigned spec assets are not inspected. Multi-image prose cannot safely be split, so
the reading path deliberately uses one native inspection request per distinct selected asset.

### Reading consumption, budgets, and reuse

`reading.visual_context` accepts `max_tokens` (default 4096) and `required` (default
false). This is a separate hard budget for the complete labeled derived-context prompt,
including identity/status metadata, using the reading plan's qualified tokenizer. It
does not reduce primary/context text budgets. Overflow fails actionably rather than
truncating uncertainty. Increase the budget in a new run if necessary.

Without a visual spec, no OAuth code runs. Every assigned asset remains explicitly
uninspected. With an optional spec, missing selections and unresolved inspections remain
limitations; successful access is still unreviewed and is not proof of correctness.
With `required: true`, every assigned primary/context asset must have a matching,
resolved inspection; absence or failure stops dispatch. Source-bundle `required` still
means required asset availability/rendering, not a requirement for model interpretation.
The current slice supports supplied small rasters whose derivative bytes match the
inspected image. Converted/oversized derivatives require the #37 follow-up; they are not
silently treated as inspected originals.

The trace is `verification/visual/<request-hash>.json` →
`derived-context/<reading-id>.json` → accepted packet `derived_context` → published
`evidence/packet-execution.json` → final `compiled-wiki-review.json`. These retain full
inspection request/configuration/version identity, original locators, bundle asset
identity, access status, generated qualifications and unresolved limitations. Worker
`coverage_notes` are also preserved as `derived_review` in publication/review, not as
source-backed claims. Inspection does not earn a human-reviewed status (ADR-001/004).

The worker execution fingerprint includes its own context file. Reuse compares exact
consumed context, including the model instructions and inspection checksum. Incompatible
packets are recollected; same-run context drift fails with instructions to preserve
history and start a new run. Historical inspection/context files are never overwritten.
Missing consumed files fail publication instead of silently dropping the binding.

Notes never enter original snapshots, frozen primary-source representations, or claim
excerpts. A generated transcription fails quotation grounding unless independently
present in original textual source. Visual-only propositions belong in limitations for
independent review, not fabricated textual citations. Source hashes, stable claim IDs,
and primary/context independence grouping remain unchanged.

### Bounded-compilation boundary (#40)

This slice feeds actual reading research and published review context, not native
compilation: `compiler_received: false` describes only this derived-context path.
Native source-bundle image handling is separate. #40 should consume accepted per-reading
`derived_context` through the supported research-context mechanism, retain its hash and
unreviewed label, and record actual delivery before changing this status. Never inject
notes into compiler source text or imply that a compiler received pixels or independent
primary evidence from this record.

The inspection record includes:

- Immutable request identity: model, effort, instructions, exact dependency versions,
  adapter revision, asset paths/hashes and original locators.
- Separate `available`, `rendered`, `inspected`, and `reviewed` values. Supplied rasters
  are not rendered by this module; a native image result proves access only. Human
  review remains false. Rendering/source-bundle policy belongs to #12.
- Original image call IDs and argument strings, image/error result association,
  original/derivative byte hashes, generated note, and explicit unresolved reason.
- A record checksum. The same request reuses its captured note, including unresolved
  records. Changed assets/configuration require a new record path. This is an advisory
  artifact checksum, not a replacement for immutable source manifests.

Any missing/failed required image, absent final answer, unsupported response shape,
duplicate call ID, or native exception leaves visual coverage unresolved. Optional
visual uncertainty does not gate text publication or masquerade as complete coverage.
The record omits image payloads and provider exception bodies; Agents tracing and
LiteLLM message logging are disabled in the CLI.

## Compatibility and removal

Checked 2026-09-18: the latest [OpenKB release remains v0.4.5](https://github.com/VectifyAI/OpenKB/releases/tag/v0.4.5).
The [current upstream LiteLLM converter](https://github.com/BerriAI/litellm/blob/main/litellm/completion_extras/litellm_responses_transformation/transformation.py)
still places text and calls from one Responses output list in separate choices.
No tested compatible upstream release was identified. The owned correction is pinned
to OpenKB 0.4.5, LiteLLM 1.87.2, openai-agents 0.17.3, and openai 2.44.0.

`_oauth_visual.py` patches only the installed Responses converter inside this dedicated
process, restoring it afterward. It validates completed envelopes/output items,
combines sequential output items, preserves reasoning/annotations and call IDs, and
rejects genuine alternative choices instead of merging generations. Explicit
`chatgpt/responses/<model>` routing avoids stale model metadata selecting Chat
Completions; `allowed_openai_params=["reasoning_effort"]` preserves configured effort.
Only non-streaming runner use is supported. Native provider SSE collection beneath
that boundary remains native behavior.

Do not call this adapter concurrently with unrelated providers in the same process.
#15 may supply resolved model/effort/instructions; it owns execution profiles and
fallback policy. This adapter does not define another execution framework.

Before upgrading, run the offline suite against proposed dependency pins. Remove the
converter override when the unpatched native path passes the text-plus-calls regression;
retain completion/inspection records and unsupported-mode checks. Never edit installed
packages, broaden global provider behavior, or replace native tools to make tests pass.

## Validation and optional live smoke

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True /tmp/visual-env/bin/python -m pytest tests/test_visual_preflight_native.py tests/test_visual_reading_native.py
```

Offline tests replay the external Responses endpoint and authentication only. Real
LiteLLM request translation, installed converter, native query agent, native tools,
and native runner execute. Network calls fail the test. The unpatched installed
combination demonstrably returns the preamble without invoking the image tool.
CI always uses the offline suite. Reading integration additionally installs `.[reading]`
and provisions the official tokenizer data before tests (see `docs/reading-plans.md`).
Its fixture draws an equation raster, replays native inspection replies, executes the
production planning/worker CLIs in-process so external endpoint patches remain active,
captures actual SDK request bodies, and publishes a prepared candidate through real
publication gates and Git commit verification. The compiler output is a fixture; no
live model/account is required and no test claims that fixture performed compilation.

A live smoke test is explicitly opt-in, uses a tiny synthetic raster, and never falls
back to an API account. It is not run by default and requires a configured OAuth account:

```sh
RESEARCH_VISUAL_LIVE_SMOKE=1 RESEARCH_VISUAL_MODEL=chatgpt/gpt-6-astra \
  /tmp/visual-env/bin/python -m pytest tests/test_visual_preflight_native.py -k live_oauth_smoke
```
