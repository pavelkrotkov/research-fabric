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
from pathlib import Path

from openai import OpenAI

from research_fabric._execution_journal import complete, configure, configured, history, reserve
from research_fabric._execution_profiles import PROVIDERS, ExecutionError, _encode, _identity, resolve


class InvalidOutput(ExecutionError):
    def __init__(self):
        super().__init__("invalid_output")


def worker_session(root, role, task, sources, project=None):
    if not (Path(root) / "execution.sqlite3").exists():
        configure(root, resolve(project))
    return Session(root, role, task, sources)


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


def _connection_failure(exc):
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in (
        "APITimeoutError",
        "APIConnectionError",
    )


def _usage(response):
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
                return self._call_once(profile, messages, validate, max_tokens)
            except ExecutionError as exc:
                if exc.reason not in ("transient", "invalid_output"):
                    raise
        raise ExecutionError("attempt_limit")

    def _call_once(self, profile, messages, validate, max_tokens):
        attempt, timeout = self.begin(profile)
        started, response, outcome = time.monotonic(), None, "configuration_or_transport"
        try:
            kwargs = self.arguments(profile, timeout, max_tokens)
            response = _client(profile, timeout).chat.completions.create(messages=messages, **kwargs)
            result = _validated(response, validate)
            outcome = "accepted"
            return result
        except Exception as exc:
            outcome = classify(exc)
            raise ExecutionError(outcome) from None
        finally:
            self.finish(attempt, response, outcome, time.monotonic() - started)

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
