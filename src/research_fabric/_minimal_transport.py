"""Bounded native subscription transport, installed only inside the minimal child.

No credentials/prompts are persisted, no login/refresh or paid fallback is allowed.
Unknown usage retains its conservative reservation and blocks further calls.
"""

import hashlib
import inspect
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def check_budget(run):
    from ._openkb045 import CompilationError

    state = json.loads((Path(run) / "budget.json").read_text())
    records = [json.loads(path.read_text()) for path in sorted(Path(run).glob("request-*.json"))]
    if state.get("inflight") or state.get("stopped"):
        raise CompilationError("Unresolved or terminal subscription attempt; budget cannot be reset")
    if (
        state["requests"] != len(records)
        or state["tokens"] != sum(r["charged_tokens"] for r in records)
        or state["observed_tokens"] != sum(r["observed_tokens"] or 0 for r in records)
    ):
        raise CompilationError("Budget counters differ from recorded attempts")
    return state


@contextmanager
def subscription(run, settings):
    import httpx
    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import GetAccessTokenError

    from ._openkb045 import CompilationError

    run = Path(run)
    if hashlib.sha256(Path(inspect.getfile(Authenticator)).read_bytes()).hexdigest() != (
        "c24bbf65253d626f205ae63a0d4f744eaea1207a0c2ebdd2320ea2d390f26983"
    ):
        raise CompilationError("Subscription authenticator differs from qualified LiteLLM; requalify before use")
    budget_file = run / "budget.json"
    state = check_budget(run)

    def initialize(self):
        data = json.loads(Path(settings["auth_file"]).read_text())
        if data.get("auth_mode") != "chatgpt" or not data.get("tokens", {}).get("access_token"):
            raise GetAccessTokenError("Existing Codex subscription sign-in required")
        self._minimal_auth = dict(data["tokens"])
        self._minimal_auth["expires_at"] = self._get_expires_at(self._minimal_auth["access_token"])

    def prohibit(*args, **kwargs):
        raise GetAccessTokenError("Refresh the existing sign-in outside this run")

    def reserve(request):
        if state.get("stopped") or state.get("inflight"):
            raise CompilationError("Subscription execution stopped")
        if str(request.url) != "https://chatgpt.com/backend-api/codex/responses":
            state["stopped"] = "endpoint_mismatch"
            write_json(budget_file, state)
            raise CompilationError("Unapproved endpoint; subscription-only execution")
        body = json.loads(request.content)
        if body.get("model") != settings["model"] or body.get("reasoning", {}).get("effort") != settings["effort"]:
            state["stopped"] = "model_or_effort_mismatch"
            write_json(budget_file, state)
            raise CompilationError("Model or effort differs from frozen settings")
        # UTF-8 input bytes plus framing and the qualified model's maximum
        # output allowance; this is accounting, not an API generation cap.
        reserved = len(request.content) + 4096 + 131072
        if state["requests"] >= settings["max_requests"] or state["tokens"] + reserved > settings["max_tokens"]:
            state["stopped"] = "budget_exhausted"
            write_json(budget_file, state)
            raise CompilationError("Subscription budget exhausted")
        state.update(requests=state["requests"] + 1, tokens=state["tokens"] + reserved, inflight=True)
        write_json(budget_file, state)
        return reserved

    def finish(reserved, response=None):
        usage = None
        reported_model = None
        failed = None
        if response is not None and response.status_code == 200:
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    continue
                if event.get("type") in ("response.failed", "response.incomplete", "error"):
                    failed = "failed_response"
                if event.get("type") == "response.completed":
                    payload = event["response"]
                    usage, reported_model = payload.get("usage"), payload.get("model")
        total = (usage or {}).get("total_tokens")
        if type(total) is int and total >= 0:
            state["tokens"] += total - reserved
            state["observed_tokens"] += total
            if total > reserved or (usage.get("output_tokens") or 0) > settings["output_tokens"]:
                failed = "token_acceptance_limit"
        else:
            failed = failed or "unknown_usage"
        if response is not None and response.status_code in (401, 403, 429):
            failed = "authentication_or_quota"
        if reported_model != settings["model"]:
            failed = failed or "model_not_confirmed"
        state["inflight"] = False
        if failed:
            state["stopped"] = failed
        write_json(budget_file, state)
        write_json(
            run / f"request-{state['requests']:05}.json",
            {
                "id": state["requests"],
                "status": response.status_code if response is not None else None,
                "model": reported_model,
                "effort": settings["effort"],
                "observed_tokens": total,
                "charged_tokens": total if type(total) is int and total >= 0 else reserved,
                "stop_reason": failed,
            },
        )
        if failed:
            raise CompilationError(f"Subscription stopped: {failed}")

    original_send, original_async = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, *args, **kwargs):
        reserved = reserve(request)
        response = None
        try:
            # Redirect hops happen inside httpx and bypass our request guard.
            kwargs["follow_redirects"] = False
            response = original_send(self, request, *args, **kwargs)
            response.read()
        finally:
            finish(reserved, response)
        return response

    async def send_async(self, request, *args, **kwargs):
        reserved = reserve(request)
        response = None
        try:
            kwargs["follow_redirects"] = False
            response = await original_async(self, request, *args, **kwargs)
            await response.aread()
        finally:
            finish(reserved, response)
        return response

    # ponytail: serial child only; use separate runs rather than global patches
    # in a server. Pin/qualify these six native authentication seams on upgrades.
    patches = {
        "__init__": initialize,
        "_read_auth_file": lambda self: dict(self._minimal_auth),
        "_write_auth_file": lambda self, value: setattr(self, "_minimal_auth", dict(value)),
        "_login_device_code": prohibit,
        "_wait_for_access_token": prohibit,
        "_refresh_tokens": prohibit,
    }
    with ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(patch.object(Authenticator, name, value))
        stack.enter_context(patch.object(httpx.Client, "send", send))
        stack.enter_context(patch.object(httpx.AsyncClient, "send", send_async))
        yield
        if state.get("stopped") or state.get("inflight"):
            raise CompilationError("Subscription execution did not finish cleanly")
