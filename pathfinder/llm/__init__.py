"""LLM adapter surface: resolve a "provider:model" reference to a concrete
Endpoint. Adding a provider is a config entry, not a new module - the loop
and daemon never see a model name."""
import os
from dataclasses import dataclass

from .. import keychain


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str
    model: str
    api: str = "openai"  # which wire format: "openai" (default) or "anthropic" (D2, D37 - experimental)
    effort: str = ""     # providers.<name>.effort (anthropic only); "" = the API default


# Replayed only to the model that wrote them; `reasoning` (openai_compat) always comes from another model.
THINK_TYPES = ("thinking", "redacted_thinking", "reasoning")


@dataclass(frozen=True)
class Request:
    role: str               # a `models` key: root | worker | reviewer | triage | summarize | night
    system: str             # piece 1, byte-frozen
    tools: tuple = ()       # piece 2: {"name", "description", "parameters"}; read_result first, rest by name
    messages: tuple = ()    # pieces 3-5: {"role": "user", "content": [text | tool_result]} ·
                            # {"role": "assistant", "model": ref, "content": [blocks]} · {"role": "system", "content": resident, "item": turn}
    model: str = ""         # spawn override "provider:model"; "" = the role's chain
    max_tokens: int = 16000  # root; 2,000 for summarize
    clone: str = ""         # with `turn`, pins the router's chain position for this run
    turn: str = ""


@dataclass
class Response:
    blocks: list            # verbatim (thinking with signature)
    stop_reason: str        # end_turn | tool_use | max_tokens | refusal | interrupted
    usage: dict             # input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
    model: str              # the "provider:model" that answered
    fallbacks: list         # [{model, status, message}] tried before it
    stop_details: dict | None = None  # on refusal, the provider's details when it gives them (anthropic)


class LLMError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def blocked(self):
        # 402 = out of credit, 429 = rate limited: both mean "try later or
        # switch model", not "the code is broken".
        return self.status in (402, 429)


def resolve(cfg, ref):
    """`ref` is "provider:model-name", split on the FIRST colon (model names
    can contain colons, e.g. tags). Empty ref or empty model is a config
    error, not a crash - the message tells the user how to fix it."""
    provider, _, model = (ref or "").partition(":")
    if not model:
        raise LLMError(0, "no model configured; set one with `pathfinder config set model provider:name`")
    providers = cfg.get("providers", {})
    if provider not in providers:
        raise LLMError(0, f"unknown provider {provider!r}; set model with `pathfinder config set model provider:name`")
    p = providers[provider]
    api = p.get("api", "openai")
    if api not in ("openai", "anthropic"):
        raise LLMError(0, f"unknown api {api!r} for provider {provider!r}; "
                           f'set providers.{provider}.api to "openai" or "anthropic"')
    api_key_env = p.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if not api_key and api == "anthropic":  # a local (openai-style) provider needs no key: never ask the Keychain
        api_key = keychain.secret(f"pathfinder-{provider}", "api") or ""
    return Endpoint(base_url=p["base_url"], api_key=api_key, model=model, api=api, effort=p.get("effort", ""))


def stream(endpoint, messages, tools=None, cancel=None, timeout=120):
    """Delegate to the adapter named by `endpoint.api` (D2: two adapters, one (kind, payload)
    contract - the loop and the slot never know which). v1 path, gone in U9: the Anthropic
    adapter has no `stream` any more; from U5 on the router serves `api: anthropic`."""
    if endpoint.api == "anthropic":
        raise LLMError(0, "api anthropic is served by the router (v2)")
    from . import openai_compat as mod
    yield from mod.stream(endpoint, messages, tools=tools, cancel=cancel, timeout=timeout)
