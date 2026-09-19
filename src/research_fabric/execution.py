"""Recorded execution profiles and one cross-process budget for a research run.

SQLite transactions reserve requests before transport. Started calls without an
outcome block resume: an operator must establish cancellation, never race a live
request. Records contain identities and counts, never prompts or exception bodies.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAI

from research_fabric._execution_journal import complete, configure, configured, history, reserve
from research_fabric._execution_profiles import PROVIDERS, ExecutionError, _encode, _identity, allowed_models, resolve


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
        complete(self.root, attempt, outcome, actual, _usage(response), elapsed, self.config["budget"])

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
                    call.response = _client(profile, call.timeout).chat.completions.create(messages=messages, **kwargs)
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
    try:
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length" or not choice.message.content:
            raise InvalidOutput()
        return validate(choice.message.content)
    except (ValueError, TypeError, KeyError, IndexError, InvalidOutput):
        raise InvalidOutput() from None


def packet_policy_defects(packet, project):
    """Keep historical identities; only the active restriction changes reuse.

    Missing returned identity is permitted, but a restricted project needs a
    recorded requested route for original extraction. Known returned aliases
    must be explicitly allowed; no guessed provider/version normalization.
    Failed/advisory attempts do not establish evidence-model provenance.
    """
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
    profile = row.get("profile")
    if not isinstance(profile, dict) or profile.get("model") not in allowed:
        return ["recorded requested model violates project restriction"]
    actual = row.get("actual_model")
    if actual is not None and actual not in allowed:
        return ["recorded returned model violates project restriction"]
    return []


def _accepted_attempts(records):
    """Distinguish malformed lineage (None) from no accepted responses ([]).

    Failed calls stay in immutable history but did not generate accepted claims;
    including them in compatibility checks would invalidate successful fallback.
    """
    if not isinstance(records, list):
        return None
    accepted = []
    for row in records:
        if not isinstance(row, dict):
            return None
        if row.get("outcome") == "accepted":
            accepted.append(row)
    return accepted
