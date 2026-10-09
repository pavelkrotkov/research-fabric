"""Recorded execution profiles and one cross-process budget for a research run.

SQLite transactions reserve requests before transport. Started calls without an
outcome block resume: an operator must establish cancellation, never race a live
request. Records contain identities and counts, never prompts or exception bodies.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAI

PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}
DEFAULT_PROFILE = {"provider": "openrouter", "model": "z-ai/glm-5.3-flash", "auth": "api_key", "account": "default"}
DEFAULT_BUDGET = {
    "requests": 100,
    "seconds": 7200,
    "tokens": 2000000,
    "attempt_seconds": 300,
    "output_tokens": 20000,
    "attempt_tokens": 100000,
    "attempts": 3,
}
AENEID_MODELS = {"deepseek-ai/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash-0731", "z-ai/glm-5.3-flash"}


class ExecutionError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(
            f"Execution stopped: {reason}; inspect execution records and explicitly resume with a valid profile"
        )


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,180}", value):
        raise ExecutionError("invalid_identity")
    return value


def _profile(value, allowed=None, native=False):
    """Whitelist persisted settings: credentials and arbitrary options stay out.

    Native routes retain OpenKB's provider/auth implementation. ChatGPT text
    calls share the qualified native Responses transport across all three roles.
    None means no model restriction; an empty allowed set permits no model.
    """
    if set(value) - {"model", "provider", "auth", "account", "effort"}:
        raise ExecutionError("unsupported_profile_setting")
    profile = {**DEFAULT_PROFILE, **value}
    for key in ("model", "provider", "auth", "account"):
        _identity(profile[key])
    if profile["account"] != "default":
        raise ExecutionError("unsupported_account")
    _validate_route(profile, native)
    if allowed is not None and profile["model"] not in allowed:
        raise ExecutionError("project_model_restriction")
    _effort(profile)
    return profile


def _validate_route(profile, native):
    if profile["provider"] == "chatgpt" and profile["auth"] == "oauth":
        if profile["model"] not in {"gpt-5", "gpt-6-astra"}:
            raise ExecutionError("unsupported_subscription_model")
        return
    if native:
        allowed = {"chatgpt": {"oauth"}, **{name: {"native", "api_key"} for name in PROVIDERS}}
        if profile["auth"] not in allowed.get(profile["provider"], {"native"}):
            raise ExecutionError("unsupported_native_provider_auth_combination")
    elif profile["provider"] not in PROVIDERS or profile["auth"] != "api_key":
        raise ExecutionError("unsupported_provider_or_auth")


def _effort(profile):
    effort = profile.get("effort")
    if effort is None:
        return
    # Deliberately qualify only the existing OpenAI-compatible reasoning route.
    model = profile["model"].removeprefix("openai/")
    qualified = model.startswith(("gpt-5", "o3", "o4")) or (profile["provider"] == "chatgpt" and model == "gpt-6-astra")
    if effort not in ("low", "medium", "high") or not qualified:
        raise ExecutionError("unsupported_reasoning_effort")
    if profile["provider"] not in ("openai", "openrouter", "chatgpt"):
        raise ExecutionError("unsupported_reasoning_provider")


def _merge(base, override):
    result = json.loads(_encode(base))
    for key, value in override.items():
        result[key] = _merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def resolve(project=None, override=None, environ=None, native_model=None, previous=None):
    """Defaults < project < previous revision < environment < explicit JSON.

    Resume inherits the previous effective profile until explicitly overridden.
    Restrictions belong to the project and are never overridden by a run.
    Fallback profiles are complete explicit routes, not credential discovery.
    """
    project, environ = project or {}, os.environ if environ is None else environ
    base = {"roles": {"extraction": DEFAULT_PROFILE, "repair": DEFAULT_PROFILE}, "budget": DEFAULT_BUDGET}
    if native_model:
        provider, model = _native_route(native_model)
        base["roles"]["compile"] = {
            **DEFAULT_PROFILE,
            "provider": provider,
            "model": model,
            "auth": "oauth" if provider == "chatgpt" else "native",
        }
    config = _merge(_merge(base, project.get("execution", {})), previous or {})
    env = _environment(environ)
    config = _merge(_merge(config, env), override or {})
    return _validate_config(config, project)


def _environment(environ):
    env = json.loads(environ.get("RESEARCH_FABRIC_EXECUTION", "{}"))
    for key in ("model", "provider"):
        if value := environ.get(f"RESEARCH_FABRIC_WORKER_{key.upper()}"):
            env.setdefault("roles", {}).setdefault("extraction", {})[key] = value
    return env


def _native_route(model):
    provider, slash, name = model.partition("/")
    if slash:
        return provider, name
    return "openai", model


def _validate_config(config, project):
    """Apply immutable project restrictions after every override is resolved.

    A fallback is validated exactly like the initial profile, including role
    restrictions; merely possessing a provider key never creates an alternative.
    """
    if set(config) - {"roles", "budget", "fallbacks", "subscription_only"}:
        raise ExecutionError("unknown_execution_setting")
    if set(config["roles"]) - {"extraction", "repair", "compile"}:
        raise ExecutionError("unknown_execution_role")
    allowed = allowed_models(project)
    config["roles"] = {
        role: _profile(p, allowed if role != "compile" else None, native=role == "compile")
        for role, p in config["roles"].items()
    }
    _fallbacks(config, allowed)
    if type(config.get("subscription_only", False)) is not bool:
        raise ExecutionError("subscription_only_requires_boolean")
    if config.get("subscription_only"):
        profiles = list(config["roles"].values()) + [p for rows in config["fallbacks"].values() for p in rows]
        if any(p["provider"] != "chatgpt" or p["auth"] != "oauth" for p in profiles):
            raise ExecutionError("subscription_only_requires_oauth_routes")
    _validate_budget(config["budget"])
    return config


def allowed_models(project):
    """One project policy for proposed calls and historical evidence alike.

    An absent restriction permits any otherwise qualified model; an explicit
    empty collection permits none. Aeneid's binding constraints intersect the
    project list, so an empty intersection must never become unrestricted.
    Entries use the same identity contract as requested model routes; mappings
    and scalar strings are not collections of authorized identities. Validate
    before intersection so malformed policies cannot disappear into an empty set.
    """
    allowed = project.get("allowed_models")
    if allowed is not None:
        if not isinstance(allowed, list):
            raise ExecutionError("allowed_models_requires_list")
        for model in allowed:
            _identity(model)
    if project.get("project") == "aeneid":
        allowed = AENEID_MODELS if allowed is None else set(allowed) & AENEID_MODELS
    return allowed


def _fallbacks(config, allowed):
    fallback = config.setdefault("fallbacks", {})
    for role, profiles in fallback.items():
        if role not in config["roles"] or not isinstance(profiles, list) or len(profiles) > 2:
            raise ExecutionError("invalid_fallback_list")
        fallback[role] = [
            _profile(p, allowed if role != "compile" else None, native=role == "compile") for p in profiles
        ]


def _validate_budget(budget):
    if set(budget) != set(DEFAULT_BUDGET):
        raise ExecutionError("unknown_budget_setting")
    _positive_limits(budget.values())
    for key in ("requests", "tokens", "output_tokens", "attempt_tokens", "attempts"):
        if not isinstance(budget[key], int):
            raise ExecutionError("budget_requires_integer")
    if budget["attempts"] > 3:
        raise ExecutionError("at_most_three_attempts")


def _positive_limits(values):
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ExecutionError("invalid_budget")


@contextmanager
def _connect(root):
    path = Path(root) / "execution.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and tables != {"revisions", "attempts"}:
            raise ExecutionError("unsupported_execution_journal_preserve_and_start_new_run")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, revision INTEGER, role TEXT, task TEXT,
                source TEXT, profile TEXT, started REAL, timeout REAL, outcome TEXT, actual_model TEXT,
                usage TEXT, elapsed REAL);
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if "settings" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN settings TEXT")
        with db:
            yield db
    finally:
        db.close()


def configure(root, config):
    """Record an immutable configuration revision only between requests."""
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        last = db.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
        if db.execute("SELECT 1 FROM attempts WHERE outcome IS NULL").fetchone():
            raise ExecutionError("in_flight_or_interrupted_request")
        if last and last["config"] == _encode(config):
            return last["id"]
        return db.execute("INSERT INTO revisions(config) VALUES (?)", (_encode(config),)).lastrowid


def configured(root):
    with _connect(root) as db:
        row = db.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise ExecutionError("run_not_configured")
    return row["id"], json.loads(row["config"])


def _known_tokens(rows):
    return sum(json.loads(row["usage"]).get("total_tokens") or 0 for row in rows if row["usage"])


def _budget_outcome(db, budget, usage, elapsed, timeout, outcome):
    tokens = usage["total_tokens"] or 0
    used = _known_tokens(db.execute("SELECT usage FROM attempts").fetchall())
    exceeded = (
        elapsed > timeout,
        tokens > budget["attempt_tokens"],
        used + tokens > budget["tokens"],
        (usage["completion_tokens"] or 0) > budget["output_tokens"],
    )
    return "budget_exhausted" if any(exceeded) else outcome


def _remaining(records, budget):
    used_time = sum(r["elapsed"] if r["outcome"] else r["timeout"] for r in records)
    used_tokens = _known_tokens(records)
    if len(records) >= budget["requests"] or used_time >= budget["seconds"] or used_tokens >= budget["tokens"]:
        raise ExecutionError("budget_exhausted")
    return min(budget["attempt_seconds"], budget["seconds"] - used_time)


def history(root):
    with _connect(root) as db:
        revisions = [
            {"revision": r["id"], "config": json.loads(r["config"])} for r in db.execute("SELECT * FROM revisions")
        ]
        rows = [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]
    for row in rows:
        row["profile"] = json.loads(row["profile"])
        row["usage"] = json.loads(row["usage"]) if row["usage"] else None
        row["settings"] = json.loads(row["settings"]) if row["settings"] else None
    return {
        "revisions": revisions,
        "attempts": rows,
        "unknown_usage_calls": sum(not r["usage"] or r["usage"]["total_tokens"] is None for r in rows),
    }


def reserve(root, record, budget):
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("SELECT max(id) FROM revisions").fetchone()[0]
        if latest != record[0]:
            raise ExecutionError("configuration_changed_at_checkpoint")
        records = db.execute("SELECT * FROM attempts").fetchall()
        timeout = _remaining(records, budget)
        attempt = db.execute(
            "INSERT INTO attempts(revision,role,task,source,profile,started,timeout) VALUES (?,?,?,?,?,?,?)",
            (*record, time.time(), timeout),
        ).lastrowid
    return attempt, timeout


def complete(root, attempt, outcome, actual, usage, elapsed, budget, settings=None):
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT timeout FROM attempts WHERE id=? AND outcome IS NULL", (attempt,)).fetchone()
        if row is None:
            raise ExecutionError("attempt_missing_or_already_completed")
        outcome = _budget_outcome(db, budget, usage, elapsed, row["timeout"], outcome)
        updated = db.execute(
            "UPDATE attempts SET outcome=?, actual_model=?, usage=?, elapsed=?, settings=? "
            "WHERE id=? AND outcome IS NULL",
            (outcome, actual, _encode(usage), elapsed, _encode(settings), attempt),
        )
        if updated.rowcount != 1:
            raise ExecutionError("attempt_missing_or_already_completed")
    if outcome == "budget_exhausted":
        raise ExecutionError(outcome)


class InvalidOutput(ExecutionError):
    def __init__(self):
        super().__init__("invalid_output")


def worker_session(root, role, task, sources, project=None):
    """Validate the caller's project even when the run was configured earlier.

    Existing settings are checked, not relabeled or replaced. Only workflow
    configuration creates revisions; worker entry cannot bypass restrictions
    by inheriting a database created for another task.
    """
    if not (Path(root) / "execution.sqlite3").exists():
        configure(root, resolve(project))
    session = Session(root, role, task, sources)
    resolve(project, previous=session.config, environ={})
    return session


def classify(exc):
    if isinstance(exc, ExecutionError):
        return exc.reason
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "authentication"
    if status in {429, *range(500, 600)}:
        return "transient"
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in (
        "APITimeoutError",
        "APIConnectionError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectError",
    ):
        return "transient"
    return "configuration_or_transport"


def _usage(response):
    """Missing or malformed usage is unknown, never zero-cost evidence.

    Derive a total only when both components are known. Failed/invalid responses
    retain reported usage too; the journal charges it before another call.
    """
    usage = getattr(response, "usage", None)
    result = {
        key: _token_count(getattr(usage, key, None)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    if result["total_tokens"] is None and all(result[k] is not None for k in ("prompt_tokens", "completion_tokens")):
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def _token_count(value):
    return value if type(value) is int and value >= 0 else None


def _key(provider):
    """Read the operator-selected provider secret without discovering a route.

    The environment file is local operator configuration, never source/model
    input; loading a different provider's key cannot create a fallback profile.
    """
    name = PROVIDERS[provider][1]
    key = os.environ.get(name)
    path = Path(os.environ.get("RESEARCH_FABRIC_ENV_FILE", str(Path.home() / ".hermes/.env")))
    if not key and path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith(name + "="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise ExecutionError("credential_unavailable")
    return key


def _client(profile, timeout):
    if profile["account"] != "default":
        raise ExecutionError("unsupported_account")
    return OpenAI(
        base_url=PROVIDERS[profile["provider"]][0], api_key=_key(profile["provider"]), timeout=timeout, max_retries=0
    )


def response(profile, messages, arguments):
    if profile["provider"] == "chatgpt":
        from research_fabric._oauth_execution import response as oauth_response

        return oauth_response(profile, messages, arguments["timeout"])
    return _client(profile, arguments["timeout"]).chat.completions.create(messages=messages, **arguments)


class Session:
    """One task's frozen profile revision; the journal owns run-wide limits.

    `call` returns only after the response validator and atomic usage acceptance
    succeed. Its returned value is still a proposal: packet/source/publication
    gates remain mandatory. An attempt's source fingerprint describes inputs,
    never proves provenance or authorizes reuse. `records` embeds the requested
    route and observed identity, so copied artifacts do not inherit a new model.
    """

    def __init__(self, root, role, task, sources):
        self.root, self.role, self.task = Path(root), _identity(role), _identity(task)
        self.revision, self.config = configured(root)
        self.source = hashlib.sha256(
            _encode(sorted((Path(p).name, hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in sources)).encode()
        ).hexdigest()
        self.ids = []

    def profiles(self):
        return [self.config["roles"][self.role], *self.config["fallbacks"].get(self.role, [])]

    def begin(self, profile):
        record = (self.revision, self.role, self.task, self.source, _encode(profile))
        attempt, timeout = reserve(self.root, record, self.config["budget"])
        self.ids.append(attempt)
        return attempt, timeout

    def finish(self, attempt, response, outcome, elapsed):
        """Commit observed usage before any pending call result can escape.

        The journal can reject acceptance here, including from a finally block;
        that error intentionally overrides the proposed return value.
        """
        actual = getattr(response, "model", None)
        if actual is not None and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,180}", str(actual)):
            actual = None
        complete(
            self.root,
            attempt,
            outcome,
            actual,
            _usage(response),
            elapsed,
            self.config["budget"],
            getattr(response, "execution_settings", None),
        )

    def call(self, messages, validate=lambda text: text, max_tokens=None):
        """Try only configured profiles; auth/config errors require intervention.

        The validator must raise ValueError/InvalidOutput for unusable content.
        It runs before outcome acceptance, so malformed retries remain visible.
        This retry loop is for evidence only; native mutation retries belong to
        compilation.compile_with_recovery and never call this method.
        """
        profiles = self.profiles()
        for index in range(self.config["budget"]["attempts"]):
            profile = profiles[min(index, len(profiles) - 1)]
            try:
                with self.attempt(*self.begin(profile)) as call:
                    kwargs = self.arguments(profile, call.timeout, max_tokens)
                    call.response = response(profile, messages, kwargs)
                    return _validated(call.response, validate)
            except ExecutionError as exc:
                if exc.reason not in ("transient", "invalid_output"):
                    raise
        raise ExecutionError("attempt_limit")

    @contextmanager
    def attempt(self, attempt, timeout):
        """Record one reserved call, including invalid output and failed transport.

        Callers set response before validating it so rejected content is charged.
        Usage acceptance in finally must succeed before a result can escape.
        """
        call = SimpleNamespace(response=None, timeout=timeout)
        started, outcome = time.monotonic(), "configuration_or_transport"
        try:
            yield call
            outcome = "accepted"
        except Exception as exc:
            outcome = classify(exc)
            raise ExecutionError(outcome) from None
        finally:
            self.finish(attempt, call.response, outcome, time.monotonic() - started)

    def arguments(self, profile, timeout, max_tokens=None):
        result = {
            "model": profile["model"],
            "max_tokens": min(
                max_tokens or self.config["budget"]["output_tokens"], self.config["budget"]["output_tokens"]
            ),
            "timeout": timeout,
        }
        if profile.get("effort"):
            result["reasoning_effort"] = profile["effort"]
        else:
            result["temperature"] = 0
        return result

    def records(self):
        return [r for r in history(self.root)["attempts"] if r["id"] in self.ids]


def _validated(response, validate):
    if reason := getattr(response, "execution_error", None):
        raise ExecutionError(reason)
    try:
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length" or not choice.message.content:
            raise InvalidOutput()
        return validate(choice.message.content)
    except (ValueError, TypeError, KeyError, IndexError, InvalidOutput):
        raise InvalidOutput() from None


def packet_policy_defects(packet, project):
    """Validate original extraction lineage before checking accepted evidence routes."""
    allowed = allowed_models(project)
    if allowed is None:
        return []
    accepted = _accepted_attempts(packet.get("execution"))
    if accepted is None:
        return ["execution lineage missing under model restriction"]
    if not any(row.get("role") == "extraction" for row in accepted):
        return ["original extraction route unknown under model restriction"]
    return [error for row in accepted for error in _record_policy_defects(row, allowed)]


def _record_policy_defects(row, allowed):
    if row.get("role") not in ("extraction", "repair"):
        return []
    errors = []
    profile = row.get("profile")
    if not isinstance(profile, dict) or profile.get("model") not in allowed:
        errors.append("recorded requested model violates project restriction")
    actual = row.get("actual_model")
    if actual is not None and actual not in allowed:
        errors.append("recorded returned model violates project restriction")
    return errors


def _accepted_attempts(records):
    """Reject malformed lineage; failed calls did not produce accepted evidence."""
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        return None
    return [row for row in records if row.get("outcome") == "accepted"]
