"""The router with both adapters patched by fake `complete` coroutines: no network, no Keychain."""
import asyncio
import os
import unittest
from unittest import mock

from pathfinder import router
from pathfinder.llm import LLMError, Request, Response

CFG = {
    "providers": {"cloud": {"api": "anthropic", "base_url": "https://cloud.example", "api_key_env": "FAKE_KEY_ENV"},
                  "local": {"base_url": "http://127.0.0.1:1/v1"}},
    "models": {"root": ["cloud:big", "local:small", "local:tiny"], "summarize": ["local:small"]},
}


class FakeAdapters:
    """Scripted answers per bare model name: an int status raises LLMError, a str is a stop_reason."""

    def __init__(self, script):
        self.script = {model: list(steps) for model, steps in script.items()}
        self.calls = []

    async def complete(self, endpoint, req, on_text=None, cancel=None):
        self.calls.append(endpoint.model)
        step = self.script[endpoint.model].pop(0) if self.script.get(endpoint.model) else "end_turn"
        if isinstance(step, int):
            raise LLMError(step, f"fake {step}")
        return Response(blocks=[{"type": "text", "text": "hi"}], stop_reason=step, usage={},
                        model=endpoint.model, fallbacks=[])


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAdapters({})
        env = mock.patch.dict(os.environ, {"FAKE_KEY_ENV": "fake-key"})  # resolve never reaches the Keychain
        env.start()
        self.addCleanup(env.stop)
        for target in ("pathfinder.router.anthropic.complete", "pathfinder.router.openai_compat.complete"):
            patcher = mock.patch(target, self.fake.complete)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.router = router.Router(lambda: CFG)

    def run_req(self, req):
        return asyncio.run(self.router.complete(req))

    def script(self, **steps):
        self.fake.script = {model: list(s) for model, s in steps.items()}

    def test_fallback_chain(self):
        # 402 on the first request of a run: the next ref answers at once, and fallbacks hold the failure
        self.script(big=[402])
        resp = self.run_req(Request("root", "S", clone="root", turn="t1"))
        self.assertEqual(resp.model, "local:small")
        self.assertEqual(resp.fallbacks, [{"model": "cloud:big", "status": 402, "message": "fake 402"}])
        # mid-run the run stays pinned to local:small; one failure stays on it, it answers the retry
        self.script(small=[503])
        resp = self.run_req(Request("root", "S", clone="root", turn="t1"))
        self.assertEqual((resp.model, self.fake.calls[-2:]), ("local:small", ["small", "small"]))
        # two consecutive mid-run failures switch to the next ref
        self.script(small=[429, 0])
        resp = self.run_req(Request("root", "S", clone="root", turn="t1"))
        self.assertEqual(resp.model, "local:tiny")
        self.assertEqual([f["status"] for f in resp.fallbacks], [429, 0])
        # a new run (turn) starts at the top of the chain again
        self.fake.calls.clear()
        resp = self.run_req(Request("root", "S", clone="root", turn="t2"))
        self.assertEqual((resp.model, self.fake.calls), ("cloud:big", ["big"]))

    def test_400_and_refusal_do_not_fall_back(self):
        for status in (400, 404, 413):
            self.script(big=[status])
            with self.assertRaises(LLMError) as cm:
                self.run_req(Request("root", "S", clone="root", turn=f"t{status}"))
            self.assertEqual(cm.exception.status, status)
        self.script(big=["refusal"])
        resp = self.run_req(Request("root", "S", clone="root", turn="tr"))
        self.assertEqual((resp.stop_reason, resp.model, resp.fallbacks), ("refusal", "cloud:big", []))
        self.assertNotIn("small", self.fake.calls)

    def test_every_raise_carries_the_earlier_attempts(self):
        self.script(big=[503], small=[400])  # a fallback, then a non-fallback error: the 503 is not lost
        with self.assertRaises(LLMError) as cm:
            self.run_req(Request("root", "S", clone="root", turn="t1"))
        self.assertEqual((cm.exception.status, cm.exception.fallbacks),
                         (400, [{"model": "cloud:big", "status": 503, "message": "fake 503"}]))
        self.router = router.Router(lambda: {**CFG, "models": {}})
        with self.assertRaises(LLMError) as cm:  # no chain at all: raised before any attempt
            self.run_req(Request("worker", "S"))
        self.assertEqual(cm.exception.fallbacks, [])

    def test_clone_without_turn_is_always_a_first_request(self):
        for _ in range(2):  # no pin: the second request starts at the top again and switches at once
            self.script(big=[402])
            self.assertEqual(self.run_req(Request("root", "S", clone="root")).model, "local:small")
        self.assertEqual(self.fake.calls, ["big", "small", "big", "small"])
        self.assertEqual(self.router._pins, {})

    def test_all_blocked_raises_last_error_with_fallbacks(self):
        self.script(big=[529], small=[401], tiny=[502])
        with self.assertRaises(LLMError) as cm:
            self.run_req(Request("root", "S", clone="root", turn="t1"))
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual([f["model"] for f in cm.exception.fallbacks], ["cloud:big", "local:small", "local:tiny"])

    def test_response_model_is_the_ref_not_the_bare_model(self):
        self.assertEqual(self.run_req(Request("summarize", "S")).model, "local:small")
        self.assertEqual(self.run_req(Request("root", "S")).model, "cloud:big")

    def test_override_replaces_the_chain_and_is_validated(self):
        self.assertEqual(self.router.chain("root", "local:other"), ["local:other"])
        self.assertEqual(self.router.chain("root"), CFG["models"]["root"])
        for bad in ("nowhere:x", "local:", "local"):
            with self.assertRaises(LLMError):
                self.router.chain("root", bad)
        self.assertEqual(self.run_req(Request("root", "S", model="local:other")).model, "local:other")

    def test_role_without_a_chain_uses_roots(self):
        self.router = router.Router(lambda: {**CFG, "models": {"root": ["local:small", "local:tiny"]}})
        self.assertEqual(self.router.chain("summarize"), ["local:small", "local:tiny"])
        resp = self.run_req(Request("summarize", "S", max_tokens=2000))  # the fold's request
        self.assertEqual((resp.model, self.fake.calls), ("local:small", ["small"]))

    def test_no_chain_for_the_role_nor_root_raises_naming_root(self):
        self.router = router.Router(lambda: {**CFG, "models": {"worker": ["local:small"]}})
        for role in ("summarize", "root"):
            with self.assertRaises(LLMError) as cm:
                self.router.chain(role)
            self.assertIn(f"no models for role '{role}' nor for root", cm.exception.message)

    def test_role_with_its_own_chain_ignores_roots(self):
        self.assertEqual(self.router.chain("summarize"), ["local:small"])
        self.assertEqual(self.run_req(Request("summarize", "S")).model, "local:small")
        self.assertNotIn("big", self.fake.calls)

    def test_semaphore_serializes_one_api(self):
        active, peak = 0, 0

        async def slow(endpoint, req, on_text=None, cancel=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return Response([], "end_turn", {}, endpoint.model, [])

        async def two():
            r = router.Router(lambda: CFG)
            await asyncio.gather(r.complete(Request("summarize", "S")), r.complete(Request("summarize", "S")))

        with mock.patch.dict(router.CONCURRENCY, {"openai": 1}), \
                mock.patch("pathfinder.router.openai_compat.complete", slow):
            asyncio.run(two())
        self.assertEqual(peak, 1)
        with mock.patch.dict(router.CONCURRENCY, {"openai": 2}), \
                mock.patch("pathfinder.router.openai_compat.complete", slow):
            asyncio.run(two())
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
