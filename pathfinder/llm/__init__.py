"""LLM adapter surface: resolve a "provider:model" reference to a concrete
Endpoint. Adding a provider is a config entry, not a new module - the loop
and daemon never see a model name."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str
    model: str


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
    api_key_env = p.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    return Endpoint(base_url=p["base_url"], api_key=api_key, model=model)
