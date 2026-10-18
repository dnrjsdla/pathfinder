"""The router: a role's chain of "provider:model" refs from config `models`, one adapter call at a
time per api, and the fallback rules. The clone calls `complete`; nothing else picks a model."""
import asyncio
from typing import Callable

from . import llm
from .llm import anthropic, openai_compat

CONCURRENCY = {"anthropic": 4, "openai": 1}  # one semaphore per api; constants until a second value is needed
FALLBACK_STATUSES = {0, 401, 402, 403, 408, 429}  # plus every 5xx (529 included); never 400/404/413 (our bug)
MID_RUN_FAILURES = 2  # mid-run, a model is left only after this many consecutive failures


def _falls_back(status):
    return status in FALLBACK_STATUSES or 500 <= status < 600


class Router:
    def __init__(self, load_cfg: Callable[[], dict]):
        self._load_cfg = load_cfg
        self._sems = {api: asyncio.Semaphore(n) for api, n in CONCURRENCY.items()}
        self._pins = {}  # (clone, chain) -> (turn, index): the chain position of the clone's current run

    def chain(self, role: str, override: str = "") -> list[str]:
        """The refs to try in order: the override alone (a spawn argument), else the role's `models` chain;
        a role without a chain uses root's."""
        return self._chain(self._load_cfg(), role, override)

    def _chain(self, cfg, role, override):
        if override:
            provider, _, model = override.partition(":")
            if not model or provider not in cfg.get("providers", {}):
                raise llm.LLMError(0, f"bad model override {override!r}: want provider:model with a known provider")
            return [override]
        models = cfg.get("models", {})
        refs = models.get(role) or models.get("root")
        if not refs:
            raise llm.LLMError(0, f"no models for role {role!r} nor for root; set root with "
                                  "`pathfinder config set models.root '[\"provider:model\"]'`")
        return list(refs)

    async def complete(self, req: llm.Request, on_text=None, cancel=None) -> llm.Response:
        """One model step. A run (`req.clone`, `req.turn`) starts at the top of the chain; a failure on its first
        request moves to the next ref at once, later ones only after MID_RUN_FAILURES in a row. Errors outside
        the fallback set raise at once. `Response.model` is the ref that answered, `Response.fallbacks` the
        attempts that failed before it; every LLMError raised from here carries the same list as `.fallbacks`."""
        fallbacks = []
        try:
            return await self._complete(req, on_text, cancel, fallbacks)
        except llm.LLMError as e:
            e.fallbacks = fallbacks
            raise

    async def _complete(self, req, on_text, cancel, fallbacks):
        cfg = self._load_cfg()
        refs = self._chain(cfg, req.role, req.model)
        key = (req.clone, tuple(refs))
        pinned = bool(req.clone and req.turn)  # a run needs both; without them every request is a first one
        pin = self._pins.get(key)
        first = not pinned or pin is None or pin[0] != req.turn
        index, failures = 0 if first else pin[1], 0
        while True:
            ref = refs[index]
            try:
                endpoint = await asyncio.to_thread(llm.resolve, cfg, ref)  # the Keychain lookup is a subprocess
                resp = await self._call(endpoint, req, on_text, cancel)
            except llm.LLMError as e:
                if not _falls_back(e.status):
                    raise
                fallbacks.append({"model": ref, "status": e.status, "message": e.message})
                failures += 1
                if first or failures >= MID_RUN_FAILURES:
                    index, failures = index + 1, 0
                if index == len(refs):
                    raise
                continue
            if pinned:
                self._pins[key] = (req.turn, index)
            resp.model, resp.fallbacks = ref, fallbacks
            return resp

    async def _call(self, endpoint, req, on_text, cancel):
        async with self._sems[endpoint.api]:
            if endpoint.api == "anthropic":
                return await anthropic.complete(endpoint, req, on_text)
            return await openai_compat.complete(endpoint, req, on_text, cancel)
