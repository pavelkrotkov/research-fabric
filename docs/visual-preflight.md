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
`visual_python=/tmp/visual-env/bin/python`. Before fresh planning, the workflow invokes
the isolated process and appends explicitly labeled derived notes to preparation.
Reused evidence runs still record the preflight but do not rerun planning. Notes
never enter original snapshots, compiler inputs, or claim excerpts. A generated
transcription fails the existing verbatim gate unless independently grounded in the
original source. Section-packet consumption belongs to #11.

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
LITELLM_LOCAL_MODEL_COST_MAP=True /tmp/visual-env/bin/python -m pytest tests/test_visual_preflight_native.py
```

Offline tests replay the external Responses endpoint and authentication only. Real
LiteLLM request translation, installed converter, native query agent, native tools,
and native runner execute. Network calls fail the test. The unpatched installed
combination demonstrably returns the preamble without invoking the image tool.
CI always uses the offline suite.

A live smoke test is explicitly opt-in, uses a tiny synthetic raster, and never falls
back to an API account. It is not run by default and requires a configured OAuth account:

```sh
RESEARCH_VISUAL_LIVE_SMOKE=1 RESEARCH_VISUAL_MODEL=chatgpt/gpt-6-astra \
  /tmp/visual-env/bin/python -m pytest tests/test_visual_preflight_native.py -k live_oauth_smoke
```
