"""Pure profile resolution: explicit precedence, restrictions and supported routes.

The execution module is the public boundary. This private component never reads
credentials, discovers providers from credentials, or mutates run records.
"""

import json
import math
import os
import re

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

    Native routes retain OpenKB's provider/auth implementation. Evidence routes
    are the existing API clients; claiming OAuth support there would be false.
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
    if effort not in ("low", "medium", "high") or not model.startswith(("gpt-5", "o3", "o4")):
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
    if set(config) - {"roles", "budget", "fallbacks"}:
        raise ExecutionError("unknown_execution_setting")
    if set(config["roles"]) - {"extraction", "repair", "compile"}:
        raise ExecutionError("unknown_execution_role")
    allowed = allowed_models(project)
    config["roles"] = {
        role: _profile(p, allowed if role != "compile" else None, native=role == "compile")
        for role, p in config["roles"].items()
    }
    _fallbacks(config, allowed)
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
