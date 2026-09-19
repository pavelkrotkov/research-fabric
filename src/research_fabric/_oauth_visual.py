"""Narrow OpenKB 0.4.5 OAuth compatibility for a dedicated preflight process.

LiteLLM converts one Responses output list into multiple completion choices;
the native Agents consumer takes only choice zero. Combine *only* items from
that Responses list, before the native consumer. Independent alternatives
remain unsupported, never combined. Native request conversion, tool dispatch,
image results, and the next model turn remain untouched.

The converter override is process-local and restored after the query. Do not
run unrelated provider requests concurrently in this process. Exact versions
and offline native-runner regressions are the upgrade/removal boundary.
"""

from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

SUPPORTED = {"openkb": "0.4.5", "litellm": "1.87.2", "openai-agents": "0.17.3", "openai": "2.44.0"}
ADAPTER_VERSION = 1


def supported_versions():
    try:
        actual = {name: version(name) for name in SUPPORTED}
    except PackageNotFoundError as exc:
        raise RuntimeError("install the optional tests/visual-requirements.txt environment") from exc
    if actual != SUPPORTED:
        raise RuntimeError(f"visual OAuth adapter requires {SUPPORTED}; installed {actual}; rerun compatibility tests")
    return actual


def _validate_item(item, seen):
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseReasoningItem

    if not isinstance(item, (ResponseFunctionToolCall, ResponseOutputMessage, ResponseReasoningItem)):
        raise ValueError("unsupported Responses output item; visual inspection unresolved")
    if getattr(item, "status", None) not in (None, "completed"):
        raise ValueError("incomplete Responses output item")
    if isinstance(item, ResponseOutputMessage) and any(c.type != "output_text" for c in item.content):
        raise ValueError("unsupported assistant message")
    if isinstance(item, ResponseFunctionToolCall):
        _remember_call(item.call_id, seen)


def _remember_call(call_id, seen):
    # Reject both identical replays and conflicting reuse before either can run.
    if not call_id or call_id in seen:
        raise ValueError("duplicate/missing call ID (including retry); refusing tool replay")
    seen.add(call_id)


def _combined_choice(choices):
    # Only the Responses output list has sequential items. ChatCompletion choices
    # are independent alternatives: the model consumer below rejects those.
    from litellm.types.utils import Choices, Message

    parts = {"content": [], "reasoning_content": [], "tool_calls": [], "reasoning_items": [], "annotations": []}
    for choice in choices:
        for key, values in parts.items():
            value = getattr(choice.message, key, None)
            if value:
                values.extend([value] if isinstance(value, str) else value)
    for key in ("content", "reasoning_content"):
        parts[key] = "\n".join(parts[key])
    return [
        Choices(
            index=0,
            finish_reason="tool_calls" if parts["tool_calls"] else "stop",
            message=Message(role="assistant", **parts),
        )
    ]


@contextmanager
def oauth_output_adapter():
    """Restore native conversion on exit; no package edits or general choice merging."""
    supported_versions()
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        LiteLLMResponsesTransformationHandler as Handler,
    )

    original_response = Handler.transform_response
    original = Handler._convert_response_output_to_choices
    seen = set()

    def convert(output_items, handle_raw_dict_callback=None):
        for item in output_items:
            _validate_item(item, seen)
        choices = original(output_items, handle_raw_dict_callback)
        if not choices:
            raise ValueError("response contains no answer or executable calls")
        return _combined_choice(choices)

    def response(handler, model, raw_response, *args, **kwargs):
        if raw_response.status != "completed" or raw_response.incomplete_details is not None:
            raise ValueError("incomplete Responses envelope; visual inspection unresolved")
        return original_response(handler, model, raw_response, *args, **kwargs)

    Handler.transform_response = response
    Handler._convert_response_output_to_choices = staticmethod(convert)
    try:
        yield
    finally:
        Handler.transform_response = original_response
        Handler._convert_response_output_to_choices = staticmethod(original)


async def query(wiki_root, config, calls):
    """Use native read-only tools and runner; record result association without image bytes."""
    supported_versions()
    if not config["model"].startswith("chatgpt/") or "responses/" in config["model"]:
        raise ValueError("expected chatgpt/<model> OAuth configuration")
    from agents import ModelSettings, RunConfig, Runner, ToolOutputImage
    from agents.extensions.models.litellm_model import LitellmModel
    from openai.types.shared import Reasoning
    from openkb.agent.query import build_query_agent

    class SingleResponseModel(LitellmModel):
        async def _fetch_response(self, *args, **kwargs):
            if kwargs.get("stream"):
                raise ValueError("streaming visual preflight is unsupported")
            response = await super()._fetch_response(*args, **kwargs)
            if len(response.choices) != 1:
                raise ValueError("alternative choices unsupported; refusing to merge independent generations")
            return response

    agent = build_query_agent(str(wiki_root), config["model"])
    agent.model = SingleResponseModel(model=config["model"].replace("chatgpt/", "chatgpt/responses/", 1))
    agent.model_settings = ModelSettings(
        reasoning=Reasoning(effort=config["effort"]),
        extra_args={"num_retries": 0, "max_retries": 0, "allowed_openai_params": ["reasoning_effort"]},
    )
    agent.instructions += "\n" + config["instructions"]
    native_image = next(tool for tool in agent.tools if tool.name == "get_image")
    invoke = native_image.on_invoke_tool

    async def recorded_image(context, arguments):
        call = {"call_id": context.tool_call_id, "name": "get_image", "arguments": arguments, "result": "error"}
        calls.append(call)
        result = await invoke(context, arguments)
        if isinstance(result, ToolOutputImage):
            call["result"] = "image"
        return result

    native_image.on_invoke_tool = recorded_image
    with oauth_output_adapter():
        result = await Runner.run(agent, config["question"], max_turns=8, run_config=RunConfig(tracing_disabled=True))
    return result.final_output or ""
