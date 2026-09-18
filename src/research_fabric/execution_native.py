"""Execution accounting at native LiteLLM transport, never a second compiler.

A failed required operation escapes to #10's disposable-candidate recovery.
Fallback selection happens only when starting another pristine candidate.
"""

import time
from contextlib import contextmanager

from research_fabric.execution import ExecutionError, Session, _validated, classify


def native_model(profile):
    return f"{profile['provider']}/{profile['model']}"


@contextmanager
def native_execution(root, sources, compiler, cli, index):
    session = Session(root, "compile", "native-compile", sources)
    profiles = session.profiles()
    profile = profiles[min(index, len(profiles) - 1)]
    original_sync, original_async = compiler.litellm.completion, compiler.litellm.acompletion
    original_config = cli.load_config
    stopped = []

    def config(path):
        value = dict(original_config(path))
        value.update(model=native_model(profile), timeout=session.config["budget"]["attempt_seconds"])
        return value

    def prepare(kwargs):
        try:
            attempt, timeout = session.begin(profile)
        except ExecutionError as exc:
            stopped.append(exc.reason)
            raise
        kwargs.update(session.arguments(profile, timeout, kwargs.get("max_tokens")))
        kwargs.update(model=native_model(profile), num_retries=0, max_retries=0)
        return attempt, time.monotonic(), timeout

    def sync(**kwargs):
        attempt, started, timeout = prepare(kwargs)
        response, outcome = None, "configuration_or_transport"
        try:
            response = original_sync(**kwargs)
            _validated(response, lambda text: text)
            outcome = "accepted"
            return response
        except Exception as exc:
            outcome = classify(exc)
            raise ExecutionError(outcome) from None
        finally:
            session.finish(attempt, response, outcome, time.monotonic() - started)

    async def asynchronous(**kwargs):
        attempt, started, timeout = prepare(kwargs)
        response, outcome = None, "configuration_or_transport"
        try:
            response = await original_async(**kwargs)
            _validated(response, lambda text: text)
            outcome = "accepted"
            return response
        except Exception as exc:
            outcome = classify(exc)
            raise ExecutionError(outcome) from None
        finally:
            session.finish(attempt, response, outcome, time.monotonic() - started)

    cli.load_config = config
    compiler.litellm.completion, compiler.litellm.acompletion = sync, asynchronous
    try:
        yield session
        if stopped:
            raise ExecutionError(stopped[0])
        if any(row["outcome"] == "budget_exhausted" for row in session.records()):
            raise ExecutionError("budget_exhausted")
    finally:
        cli.load_config = original_config
        compiler.litellm.completion, compiler.litellm.acompletion = original_sync, original_async
