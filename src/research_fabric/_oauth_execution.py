"""Noninteractive text transport using the qualified native ChatGPT adapter.

Native code owns discovery, refresh, request conversion and SSE reconstruction.
A direct HTTP call avoids LiteLLM's hidden retries/logging and keeps the entire
response body. Never install process-global patches or initiate device login.
"""

import time
from types import SimpleNamespace

from research_fabric.execution import ExecutionError


def prepare():
    from research_fabric._oauth_visual import supported_versions

    try:
        supported_versions()
    except RuntimeError:
        raise ExecutionError("unsupported_oauth_toolchain") from None
    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import CHATGPT_API_BASE
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig

    class NoninteractiveAuth(Authenticator):
        def _login_device_code(self):
            raise ExecutionError("credential_unavailable")

        def _wait_for_access_token(self, timeout_seconds):
            raise ExecutionError("credential_unavailable")

        def _refresh_tokens(self, refresh_token):
            try:
                return super()._refresh_tokens(refresh_token)
            except Exception:
                # Do not let native get_access_token log the refresh exception
                # or continue into device login after a failed refresh.
                raise ExecutionError("authentication_refresh_failed") from None

    config = ChatGPTResponsesAPIConfig()
    config.authenticator = NoninteractiveAuth()
    if config.authenticator.get_api_base().rstrip("/") != CHATGPT_API_BASE.rstrip("/"):
        raise ExecutionError("unsupported_oauth_endpoint")
    return config


def response(profile, messages, timeout):
    import httpx
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        LiteLLMResponsesTransformationHandler,
    )
    from litellm.types.router import GenericLiteLLMParams

    started = time.monotonic()
    config = prepare()
    params = GenericLiteLLMParams()
    headers = config.validate_environment({}, profile["model"], params)
    options = {"reasoning": {"effort": profile["effort"]}} if profile.get("effort") else {}
    inputs, instructions = LiteLLMResponsesTransformationHandler().convert_chat_completion_messages_to_responses_api(
        messages
    )
    if instructions:
        options["instructions"] = instructions
    payload = config.transform_responses_api_request(profile["model"], inputs, options, params, headers)
    timeout -= time.monotonic() - started
    if timeout <= 0:
        raise ExecutionError("budget_exhausted")
    # Native ChatGPT strips max_output_tokens. This route has an acceptance
    # limit, not a generation cap; the execution journal records that distinction.
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        raw = client.post(config.get_complete_url(None, {}), headers=headers, json=payload)
    if raw.status_code in (401, 403):
        raise ExecutionError("authentication")
    if raw.status_code == 429:
        # Conservatively stop; never assume subscription exhaustion is retryable.
        raise ExecutionError("subscription_quota")
    if raw.status_code >= 500:
        raise ExecutionError("transient")
    if raw.status_code != 200:
        raise ExecutionError("configuration_or_transport")
    try:
        native = _native_response(config, profile["model"], raw)
    except Exception:
        raise ExecutionError("invalid_output") from None
    result = _completion(native)
    reasoning = getattr(native, "reasoning", None)
    reported = reasoning.get("effort") if isinstance(reasoning, dict) else getattr(reasoning, "effort", None)
    result.execution_settings = {
        "sent_effort": profile.get("effort"),
        "reported_effort": reported if reported in {"low", "medium", "high"} else None,
        "output_limit": "acceptance_only",
        "temperature": "provider_default",
    }
    if reported is not None and profile.get("effort") is not None and reported != profile["effort"]:
        result.choices[0].finish_reason = "length"
    return result


def _native_response(config, model, raw):
    from litellm.responses.sse_output_recovery import parse_sse_json_chunk

    # 1.87.2 reconstructs completed events, but drops incomplete/failed envelopes.
    # Reuse its envelope builder to retain usage; never treat a failed event as
    # merely malformed output (which could select a configured paid fallback).
    for line in raw.text.splitlines():
        event = parse_sse_json_chunk(line)
        if not event or event.get("type") not in {"response.incomplete", "response.failed", "error"}:
            continue
        native = config._build_completed_response_from_chunk(event, {})
        if native is not None:
            return native
        return SimpleNamespace(status="failed", error=event.get("error") or {})
    return config.transform_response_api_response(model, raw, SimpleNamespace(post_call=lambda **kwargs: None))


def _error(native):
    error = getattr(native, "error", None)
    if not error and getattr(native, "status", None) != "failed":
        return None
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    if code in {"usage_limit_reached", "insufficient_quota", "rate_limit_exceeded"}:
        return "subscription_quota"
    if code in {"invalid_api_key", "token_expired", "invalid_token", "authentication_error"}:
        return "authentication"
    if code in {"server_error", "internal_error"}:
        return "transient"
    return "configuration_or_transport"


def _completion(native):
    """Normalize only the text view; reject unsupported/incomplete output later.

    Retain model and usage even for rejected responses so the shared journal
    charges them. Reasoning is not an answer; sequential message text is.
    """
    parts = []
    valid = getattr(native, "status", None) == "completed" and not getattr(native, "incomplete_details", None)
    for item in getattr(native, "output", None) or []:
        kind = getattr(item, "type", None)
        if kind == "reasoning":
            continue
        if kind != "message" or getattr(item, "status", None) != "completed":
            valid = False
            continue
        for content in item.content:
            if content.type != "output_text":
                valid = False
            else:
                parts.append(content.text)
    usage = getattr(native, "usage", None)
    return SimpleNamespace(
        execution_error=_error(native),
        model=getattr(native, "model", None),
        usage=SimpleNamespace(
            prompt_tokens=getattr(usage, "input_tokens", None),
            completion_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="\n".join(parts)), finish_reason="stop" if valid else "length"
            )
        ],
    )
