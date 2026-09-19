"""Execution accounting at native LiteLLM transport, never a second compiler.

A failed required operation escapes to #10's disposable-candidate recovery.
Fallback selection happens only when starting another pristine candidate.
"""

from contextlib import contextmanager
from pathlib import Path

from research_fabric.execution import ExecutionError, Session, _validated, configured, history


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
        return attempt, timeout

    def sync(**kwargs):
        with session.attempt(*prepare(kwargs)) as call:
            call.response = original_sync(**kwargs)
            _validated(call.response, lambda text: text)
            return call.response

    async def asynchronous(**kwargs):
        with session.attempt(*prepare(kwargs)) as call:
            call.response = await original_async(**kwargs)
            _validated(call.response, lambda text: text)
            return call.response

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


def _attempt_limit(root, attempts):
    if root is None:
        return attempts

    return min(attempts, configured(root)[1]["budget"]["attempts"])


def _execution_arguments(root, attempt):
    if root is None:
        return []
    return ["--execution-root", str(Path(root).resolve()), "--profile-index", str(attempt - 1)]


def _compile_checkpoint(root):
    if root is None:
        return 0

    return max((r["id"] for r in history(root)["attempts"]), default=0)
