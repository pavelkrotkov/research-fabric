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
`effort`. Only the default account is currently qualified. Evidence transport
supports OpenAI, OpenRouter and NVIDIA API keys through the existing OpenAI client.
Compilation delegates transport/auth to native OpenKB/LiteLLM, including a native
`chatgpt/` OAuth model; it does not substitute a paid API route.

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
is currently qualified only for OpenAI-compatible GPT-5/o3/o4 model routes and
`low`, `medium`, `high`; it is never silently discarded. GPT-6 Astra effort is
not yet qualified in this compile/extraction seam and is rejected explicitly;
issue #14's separately qualified visual OAuth adapter remains unchanged.
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

Only transient structured transport errors and invalid output retry. Auth and
configuration failures stop with an actionable normalized reason. Native required
operation failures return to the existing whole-candidate recovery: another
configured compile profile is selected only for a fresh candidate. Native optional
summary fallback retains its existing meaning. No in-candidate model retry is
introduced. Correcting a credential without changing the profile can resume;
old failed attempts remain recorded but do not poison the new invocation.

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
Packets and published claims carry their original execution records; a mixed-model
run has histories rather than a misleading top-level model constant. Legacy
packets without execution records remain explicitly unknown.

CAO planning/advisory calls do not expose effective model or usage through the
existing workflow seam. Native knowledge-lint advisory calls also remain native.
These are recorded/documented as unknown and are outside this evidence/compile
request budget. Their outputs remain advisory; deterministic gates alone control
publication. The optional visual tool adapter in issue #14 remains its owner's
boundary and does not gain an implicit fallback route here.
