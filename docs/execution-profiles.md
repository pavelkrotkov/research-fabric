# Recorded model execution

Extraction, witness selection, repair and native compilation share the run's
`execution.sqlite3`. This is execution accounting, not a source registry: the
immutable source manifest and deterministic gates remain the authority.

Profiles resolve in this order: built-in defaults, project `execution`, previous
run revision on resume, environment, explicit workflow `execution` JSON. Project
`allowed_models` restrictions cannot be overridden. The Aeneid restrictions also
remain enforced. The default evidence route is OpenRouter `z-ai/glm-5.3-flash` for
both extraction and repair; compilation starts from the existing OpenKB
`.openkb/config.yaml` model. Provider selection never depends on which key happens
to be present. Profiles contain `model`, `provider`, `auth`, `account` and optional
`effort`. Only the default account is currently qualified. OpenAI, OpenRouter and
NVIDIA API-key profiles remain explicit alternatives. Extraction, repair and
native compilation also support `provider: chatgpt, auth: oauth` through the
same native ChatGPT Responses conversion and authentication machinery. No route
is selected from the presence of a key; OAuth never substitutes a paid API call.

For example, start the ordinary workflow with:

```sh
cao workflow run research --run-id example \
  --input run_root="$RUN" --input source_dir="$SOURCES" \
  --input field_root="$WORKTREE" --input question='Research question' \
  --input execution='{"roles":{"extraction":{"provider":"openai","model":"gpt-5","effort":"low"},"repair":{"provider":"openrouter","model":"z-ai/glm-5.3-flash"}},"budget":{"requests":60}}'
```

The same structure is accepted in project YAML under `execution`, or as JSON in
`RESEARCH_FABRIC_EXECUTION`. Legacy `RESEARCH_FABRIC_WORKER_MODEL` and
`RESEARCH_FABRIC_WORKER_PROVIDER` affect extraction only. Unsupported profile keys,
accounts, auth modes and reasoning settings fail before calls. Reasoning effort
supports the existing OpenAI-compatible GPT-5/o3/o4 API routes and the exact
subscription models in the table below; it is never silently discarded.
GPT-6 Astra effort remains unqualified on API-key routes. Issue #14's separate
visual adapter and its native tool-conversion regressions remain unchanged.
Native auth recorded as `native` means delegated/unobserved, not a claim about
an effective billing route. Only `chatgpt` may declare OAuth here. Keys come from existing
provider environment variables or `RESEARCH_FABRIC_ENV_FILE`; they are not stored.

## Resume and configured fallback

After a completed failure, use the same run root, a clean isolated worktree, and
`--input reuse_evidence_dir="$RUN/evidence"`. Add an explicit execution override,
for example `{"roles":{"extraction":{"model":"replacement-model"}}}`. This
appends a configuration revision; accepted packets keep their original execution
records. Their structural, source, provenance and grounding gates still run.
Active model restrictions also apply to fresh and reused artifacts. Restricted
projects require recorded original extraction lineage; unknown legacy lineage
is recollected. Accepted extraction/repair requested models and any known returned
models must each appear in `allowed_models`. List returned version/provider aliases
explicitly; the engine never guesses equivalence or strips qualifiers. An absent
provider-reported model remains unknown and does not invalidate an otherwise
allowed recorded request. Failed and advisory attempts do not define evidence
model provenance. Merely changing the selected model, without imposing a new
restriction, preserves compatible historical artifacts and their original records.
Standalone Aeneid entry revalidates both new and previously configured profiles.

Workers retain the shared source-adapter representation attestations. Reuse checks
those attestations before execution policy; execution fingerprints never authorize
source reuse. Aeneid stages accept strict JSON and select only already-grounded
witness spans; a missing or invented advisory choice uses the first grounded
candidate in input order.

A failed mutating compile must follow the recovery instructions in
[compilation-attempts.md](compilation-attempts.md): never retry against the dirty
candidate or clean up a worktree by deleting unexplained changes. The same run
budget survives a new pristine candidate and configuration revision. Native
compile acceptance binds the configuration revision and execution policy bytes,
so changing profiles during compilation cannot yield an accepted stale receipt.

Automatic fallback must be listed for that role in the run configuration:

```json
{"fallbacks":{"extraction":[{"provider":"openrouter","model":"alternative","auth":"api_key","account":"default"}]}}
```

Only transient structured transport errors and invalid output retry. Subscription
HTTP 429 stops as `subscription_quota` (conservatively including throttling);
401/403 stop as `authentication`, failed refresh as `authentication_refresh_failed`,
and missing/expired unrefreshable credentials as `credential_unavailable`.
These never trigger automatic fallback, even when another profile is configured.
Auth and configuration failures require operator intervention. Native required
operation failures return to the existing whole-candidate recovery: another
configured compile profile is selected only for a fresh candidate. Native optional
summary fallback retains its existing meaning. No in-candidate model retry is
introduced. Correcting a credential without changing the profile can resume;
old failed attempts remain recorded but do not poison the new invocation.

The journal stores each call once; a guarded transaction completes its pending
row exactly once, and completed identities/outcomes cannot be rewritten. Older
pre-release journals using separate starts/outcomes tables are rejected without
modification; preserve their diagnostics and start a new run rather than resetting
their budget.

A profile revision cannot be recorded while a request is outstanding. A killed
process leaves an explicit started request without an outcome and blocks a switch.
There is deliberately no automatic cancellation inference: verify cancellation
and preserve that run's diagnostics before beginning a separate run. The current
implementation does not provide a crash-reconciliation command.

## Budgets and provenance

Defaults are 100 requests, 7,200 cumulative request-seconds, 2,000,000 known total
tokens, 300 seconds per request, 100,000 known tokens per request, 20,000 output
tokens per request and three attempts per evidence call. Set positive values in
`budget` (`requests`, `seconds`, `tokens`, `attempt_seconds`, `attempt_tokens`,
`output_tokens`, `attempts`); at most three attempts are allowed. Native recovery
also retains #10's bounded candidate limit. The SDK and LiteLLM inner retries are
disabled. Output truncation is invalid output, never an accepted partial packet.

Requests and maximum durations are reserved transactionally across processes.
Returned usage is charged even for rejected output. Known-token acceptance and
its outcome write occur in one transaction, so concurrent responses cannot both
be accepted past the shared limit. A provider can consume tokens before reporting
an over-budget response; that response is rejected and its usage retained.
Missing usage remains unknown and is counted explicitly. These are acceptance
and bounded-request controls, **not an exact monetary cost ceiling**.

Each started call records its role/task, lossless source-byte fingerprint,
configuration revision and requested route. Each outcome records normalized
reason, returned model when reported, elapsed duration and known/unknown usage.
Prompts, image payloads, raw provider errors and credentials are excluded. A
fingerprint identifies attempted inputs only; it never authorizes source reuse.
Packets retain their original execution records. Publication writes them once to
`evidence/packet-execution.json`, keyed by worker with the immutable packet revision.
Each claim already carries that worker and revision; it does not duplicate the
entire packet history. Reused packet records remain self-contained even when
attempt numbers overlap another run. Legacy packets without records remain
explicitly unknown. The run journal separately accounts for calls made in this run.

CAO planning/advisory calls do not expose effective model or usage through the
existing workflow seam. Native knowledge-lint advisory calls also remain native.
These are recorded/documented as unknown and are outside this evidence/compile
request budget. Their outputs remain advisory; deterministic gates alone control
publication. The optional visual tool adapter in issue #14 remains its owner's
boundary and does not gain an implicit fallback route here.

## Subscription-only execution

The optional qualified environment is unchanged: install the existing
`tests/visual-requirements.txt` in a separate Python 3.12+ virtual environment,
plus this project. It pins OpenKB 0.4.5, LiteLLM 1.87.2, OpenAI Agents 0.17.3,
and OpenAI SDK 2.44.0. Other combinations fail before transport and require
requalification, not edits to installed packages. The portable environment may
use newer SDKs for API-key routes; that does not qualify its OAuth route.

| Stage | Exact subscription models | Effort |
| --- | --- | --- |
| Extraction / witness selection | `gpt-5`, `gpt-6-astra` | omitted (provider default), `low`, `medium`, `high` |
| Claim repair | `gpt-5`, `gpt-6-astra` | same |
| Native text compilation | `gpt-5`, `gpt-6-astra` | same |

Qualification means offline HTTP Responses/SSE replay through the actual native
converters and worker/compiler entrypoints, not a promise of current entitlement
for every account. The exact model is sent unchanged. Reported model is recorded
unchanged; configured project restrictions still apply. Sent effort and reported
effort are recorded separately in attempt `settings`; absent reported effort is
unknown, not confirmation. A conflicting reported effort invalidates the response.
No provider temperature is sent on OAuth. Native compilation accepts Markdown/text
inputs only on this route; the engine already prepares these from its sources.
PDF/PageIndex ingestion and its independent model calls are not qualified here.

Login must already be complete using LiteLLM's native ChatGPT login, outside a
research run. The native authenticator discovers `CHATGPT_TOKEN_DIR` (default
`~/.config/litellm/chatgpt`) and `CHATGPT_AUTH_FILE` (default `auth.json`), and owns
refresh/storage. Do not copy these files into run artifacts. Research never starts
device login, waits for a login code, or switches accounts after refresh failure.
Only the native `https://chatgpt.com/backend-api/codex` endpoint is supported;
endpoint overrides to another service fail closed. Keep the selected credential
directory/account stable across resume. Account IDs and tokens are not recorded.

Start using the ordinary workflow's `execution` JSON (or project YAML), with ALL
roles explicit, including compilation even if `.openkb/config.yaml` names an API
model:

```json
{
  "subscription_only": true,
  "roles": {
    "extraction": {"provider":"chatgpt","auth":"oauth","model":"gpt-6-astra","effort":"medium"},
    "repair": {"provider":"chatgpt","auth":"oauth","model":"gpt-6-astra","effort":"medium"},
    "compile": {"provider":"chatgpt","auth":"oauth","model":"gpt-6-astra","effort":"medium"}
  }
}
```

Run the engine and workers with the qualified interpreter (set
`RESEARCH_FABRIC_WORKER_PYTHON` if needed). API-key roles or fallbacks are rejected
in subscription-only mode. The engine writes `model-stages.json`: planning,
advisory verification, optional visual preflight and native knowledge lint are
unavailable/disabled. Supplying a visual spec fails at startup instead of silently
ignoring it. Source assignments and deterministic publication gates still run;
unavailable advisory output is neither PASS nor a new hard gate. Outside this mode,
selecting OAuth for evidence alone does NOT claim the entire run is subscription-only.

After a completed failure, resume with the same run root and evidence directory
as above and an explicit override, e.g.
`{"roles":{"extraction":{"model":"gpt-5"},"repair":{"model":"gpt-5"}}}`.
All compatible accepted packets keep historical model/claim/source identity;
failed calls, known token charges and elapsed time remain cumulative. Change only
the roles you intend. Pending attempts still block resume; do not reset journals.
Existing journals gain a nullable settings column without rewriting old attempts.

Subscription-backed work shares subscription limits. It is NOT quota-independent,
free unlimited usage, or an exact monetary ceiling. In this native version,
ChatGPT strips output-token generation caps: `output_tokens` is an acceptance
limit against returned usage, recorded as `output_limit: acceptance_only`.
Missing usage remains unknown; no temperature or generation cap is falsely claimed.
The evidence/compile transport makes one HTTP request per reserved attempt, with
no SDK/model inner retries. Native credential refresh is separate from model calls.
HTTP inactivity timeouts and elapsed-time acceptance are not remote cancellation;
process death still leaves a pending attempt that requires safe refusal.

Offline checks (no account, API key or login):

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_oauth_execution.py tests/test_visual_preflight_native.py tests/test_execution.py tests/test_native_compilation.py
```

An explicit opt-in smoke spends at most one model request, prints only the safe
journal, and refuses an existing run root. CI never runs it or logs in:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python tests/subscription_smoke.py --live --model gpt-6-astra --effort low --run-root /private/new-subscription-smoke
```
