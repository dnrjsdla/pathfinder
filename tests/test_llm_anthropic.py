"""Tests for the Anthropic adapter on the official SDK. No network: `complete` runs against a fake client
(`_client` is patched in every test that calls it), and `to_wire` is pure. The `complete` tests build canned
responses with the SDK's own types, so they need the SDK and are skipped on an interpreter without it."""
import asyncio
import json
import unittest
from unittest import mock

from pathfinder.llm import Endpoint, LLMError, Request
from pathfinder.llm import anthropic as adapter

try:
    import anthropic as sdk
    import httpx2
except ImportError:
    sdk = None

EP = Endpoint(base_url="https://api.example.com", api_key="sk-fake", model="fake-model", api="anthropic")
REF = "anthropic:fake-model"
BP = {"type": "ephemeral"}
SYSTEM = "You are a test assistant."
TOOLS = ({"name": "read_result", "description": "Reopen a stored result.",
          "parameters": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"]}},
         {"name": "read_file", "description": "Read a file.",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}})
THINK = {"type": "thinking", "thinking": "plan", "signature": "sig-fake"}
CALL = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "/tmp/x"}}
RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "r:5 text 3 lines"}
RESIDENT = {"role": "system", "content": "Local time 09:00", "item": "t1"}
TURN_A = ({"role": "user", "content": [{"type": "text", "text": "Read x."}]}, RESIDENT)
TURN_B = TURN_A + ({"role": "assistant", "model": REF, "content": [THINK, CALL]},
                   {"role": "user", "content": [RESULT]}, RESIDENT)


def request(messages):
    return Request(role="root", system=SYSTEM, tools=TOOLS, messages=messages)


def without_breakpoints(messages):
    return [{**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]]}
            if isinstance(m["content"], list) else m for m in messages]


class TestToWire(unittest.TestCase):
    def test_exact_body_and_stable_json(self):
        body = adapter.to_wire(request(TURN_B), EP)
        self.assertEqual(body, {
            "model": "fake-model", "max_tokens": 16000,
            "system": [{"type": "text", "text": SYSTEM, "cache_control": BP}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Read x."}]},
                {"role": "system", "content": "Local time 09:00", "clear_at": "next_user_message"},
                {"role": "assistant", "content": [THINK, CALL]},
                {"role": "user", "content": [{**RESULT, "cache_control": BP}]},
                {"role": "system", "content": "Local time 09:00", "clear_at": "next_user_message"}],
            "thinking": {"type": "adaptive", "display": "summarized"},
            "tools": [{"name": "read_result", "description": "Reopen a stored result.",
                       "input_schema": TOOLS[0]["parameters"]},
                      {"name": "read_file", "description": "Read a file.", "input_schema": TOOLS[1]["parameters"],
                       "cache_control": BP}],
            "betas": ["mid-conversation-system-clear-at-2026-08-21"]})
        self.assertNotIn("tool_choice", body)
        self.assertEqual(json.dumps(body), json.dumps(adapter.to_wire(request(TURN_B), EP)))
        self.assertNotIn("cache_control", TURN_B[3]["content"][0])  # the Request is never mutated

    def test_resident_breakpoint_placement(self):
        msgs = adapter.to_wire(request(TURN_B), EP)["messages"]
        marked = [(i, m["role"]) for i, m in enumerate(msgs)
                  if isinstance(m["content"], list) and any("cache_control" in b for b in m["content"])]
        self.assertEqual(marked, [(3, "user")])  # the last user block, just before the trailing copy
        self.assertTrue(all(m.get("clear_at") == "next_user_message" for m in msgs if m["role"] == "system"))
        with mock.patch.object(adapter, "RESIDENT_CLEAR_AT", False):  # the fallback constant
            body = adapter.to_wire(request(TURN_B), EP)
        self.assertEqual([m for m in body["messages"] if m["role"] == "system"],
                         [{"role": "system", "content": "Local time 09:00"}])
        self.assertEqual(body["messages"][1]["role"], "system")  # after the item's opening user message
        self.assertNotIn("betas", body)
        self.assertIn("cache_control", body["messages"][-1]["content"][-1])

    def test_thinking_dropped_for_another_model(self):
        other = ({"role": "user", "content": [{"type": "text", "text": "Hi."}]},
                 {"role": "assistant", "model": "anthropic:other-model", "content": [THINK, CALL]},
                 {"role": "assistant", "model": "ollama:fake-local",
                  "content": [{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "ok"}]},
                 {"role": "user", "content": [RESULT]})
        msgs = adapter.to_wire(request(other), EP)["messages"]
        self.assertEqual(msgs[1]["content"], [CALL])
        self.assertEqual(msgs[2]["content"], [{"type": "text", "text": "ok"}])

    def test_assistant_left_empty_is_skipped(self):
        only_thought = ({"role": "user", "content": [{"type": "text", "text": "Hi."}]}, RESIDENT,
                        {"role": "assistant", "model": "anthropic:other-model", "content": [THINK]},
                        {"role": "user", "content": [{"type": "text", "text": "Again."}]}, RESIDENT)
        body = adapter.to_wire(request(only_thought), EP)
        # the copy left before a user message goes (a 400); the trailing one stays
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "user", "system"])
        self.assertIn("betas", body)
        with mock.patch.object(adapter, "RESIDENT_CLEAR_AT", False):
            body = adapter.to_wire(request(only_thought), EP)
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "user"])
        self.assertNotIn("betas", body)  # no clear_at copy left, no beta

    def test_effort_from_the_endpoint(self):
        self.assertNotIn("output_config", adapter.to_wire(request(TURN_A), EP))
        body = adapter.to_wire(request(TURN_A), Endpoint("https://api.example.com", "sk-fake", "fake-model",
                                                         "anthropic", effort="xhigh"))
        self.assertEqual(body["output_config"], {"effort": "xhigh"})

    def test_append_only_history(self):
        for clear_at in (True, False):  # primary placement and the fallback constant
            with self.subTest(clear_at=clear_at), mock.patch.object(adapter, "RESIDENT_CLEAR_AT", clear_at):
                a, b = adapter.to_wire(request(TURN_A), EP), adapter.to_wire(request(TURN_B), EP)
                self.assertEqual(json.dumps(a["system"]), json.dumps(b["system"]))
                self.assertEqual(json.dumps(a["tools"]), json.dumps(b["tools"]))
                # the history breakpoint moves forward (the multi-turn cache pattern); every other byte of A stays
                self.assertEqual(json.dumps(without_breakpoints(b["messages"])[:len(a["messages"])]),
                                 json.dumps(without_breakpoints(a["messages"])))


class FakeStream:
    """The SDK stream helper's surface the adapter uses: async context manager, async event iterator,
    `current_message_snapshot`, `get_final_message()`."""

    def __init__(self, message, texts=(), error=None, hang=False):
        self.message, self.texts, self.error, self.hang = message, texts, error, hang

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for text in self.texts:
            yield mock.Mock(type="text", text=text)
        if self.hang:
            await asyncio.Event().wait()

    @property
    def current_message_snapshot(self):
        return self.message

    async def get_final_message(self):
        return self.message


class FakeMessages:
    def __init__(self, stream):
        self.stream_obj, self.calls = stream, []

    def stream(self, **kw):
        self.calls.append(kw)
        return self.stream_obj


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


WIRE_REQUEST = httpx2.Request("POST", "https://api.example.com/v1/messages") if sdk else None


def message(content, stop_reason, **extra):
    return sdk.types.beta.BetaMessage.model_validate({
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "fake-model", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, **extra,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 40}})


@unittest.skipUnless(sdk, "the anthropic SDK is not installed")
class TestComplete(unittest.TestCase):
    def run_with(self, stream, on_text=None):
        self.beta, self.plain = FakeMessages(stream), FakeMessages(stream)
        fake = mock.Mock(messages=self.plain, beta=mock.Mock(messages=self.beta))
        with mock.patch.object(adapter, "_client", return_value=fake) as made:
            resp = asyncio.run(adapter.complete(EP, request(TURN_B), on_text))
        made.assert_called_once_with("https://api.example.com", "sk-fake")
        return resp

    def test_thinking_blocks_verbatim_and_usage(self):
        texts = []
        resp = self.run_with(FakeStream(message([THINK, {"type": "text", "text": "Hello."}], "end_turn"),
                                        texts=("Hel", "lo.")), texts.append)
        self.assertEqual(texts, ["Hel", "lo."])
        self.assertEqual(resp.blocks, [THINK, {"type": "text", "text": "Hello."}])
        self.assertEqual((resp.stop_reason, resp.model, resp.fallbacks), ("end_turn", "fake-model", []))
        self.assertEqual(resp.usage, {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900,
                                      "cache_creation_input_tokens": 40})
        self.assertEqual(self.beta.calls, [adapter.to_wire(request(TURN_B), EP)])  # beta API: the body has betas
        self.assertEqual(self.plain.calls, [])

    def test_tool_use(self):
        resp = self.run_with(FakeStream(message([CALL], "tool_use")))
        self.assertEqual((resp.blocks, resp.stop_reason), ([CALL], "tool_use"))

    def test_refusal(self):
        details = {"type": "refusal", "category": "cyber", "explanation": "declined"}
        resp = self.run_with(FakeStream(message([], "refusal", stop_details=details)))
        self.assertEqual((resp.blocks, resp.stop_reason, resp.stop_details), ([], "refusal", details))
        self.assertIsNone(self.run_with(FakeStream(message([CALL], "tool_use"))).stop_details)

    def test_status_errors_raise_llm_error(self):
        for cls, status in ((sdk.BadRequestError, 400), (sdk.APIStatusError, 402), (sdk.RateLimitError, 429)):
            with self.subTest(status=status):
                error = cls("invalid request", response=httpx2.Response(status, request=WIRE_REQUEST), body=None)
                with self.assertRaises(LLMError) as caught:
                    self.run_with(FakeStream(None, error=error))
                self.assertEqual((caught.exception.status, caught.exception.message), (status, "invalid request"))

    def test_connection_error_is_status_0(self):
        with self.assertRaises(LLMError) as caught:
            self.run_with(FakeStream(None, error=sdk.APIConnectionError(request=WIRE_REQUEST)))
        self.assertEqual(caught.exception.status, 0)

    def test_missing_key_is_a_config_error_before_any_client(self):
        keyless = Endpoint(EP.base_url, "", EP.model, api="anthropic")
        with mock.patch.object(adapter, "_client") as made, self.assertRaises(LLMError) as caught:
            asyncio.run(adapter.complete(keyless, request(TURN_B)))
        self.assertEqual(caught.exception.status, 400)  # the router does not fall back on it: the user sees it
        made.assert_not_called()

    def test_cancel_returns_blocks_as_they_arrived(self):
        partial = message([{"type": "text", "text": "Hel"}], None)
        fake = mock.Mock(beta=mock.Mock(messages=FakeMessages(FakeStream(partial, texts=("Hel",), hang=True))))

        async def scenario():
            started = asyncio.Event()
            task = asyncio.create_task(adapter.complete(EP, request(TURN_B), lambda s: started.set()))
            await started.wait()
            task.cancel()
            return await task
        with mock.patch.object(adapter, "_client", return_value=fake):
            resp = asyncio.run(scenario())
        self.assertEqual((resp.blocks, resp.stop_reason), ([{"type": "text", "text": "Hel"}], "interrupted"))


@unittest.skipUnless(sdk, "the anthropic SDK is not installed")
class TestRealSDK(unittest.TestCase):
    """The real SDK client with its transport replaced (no network): the method names, the beta route, the body
    on the wire and the errors raised while iterating are the SDK's own, not a fake's."""
    START = {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "fake-model", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 0}}}

    def run_over(self, content):
        seen = []

        def handler(r):
            seen.append((r.url.path, r.headers.get("anthropic-beta"), json.loads(r.content)))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        client = sdk.AsyncAnthropic(api_key="sk-fake", base_url="https://api.example.com", max_retries=0,
                                    http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        with mock.patch.object(adapter, "_client", return_value=client):
            return asyncio.run(adapter.complete(EP, request(TURN_B))), seen

    def test_wire_body_and_route(self):
        resp, seen = self.run_over(sse(
            self.START, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"}))
        self.assertEqual((resp.blocks, resp.stop_reason), ([{"type": "text", "text": "Hi"}], "end_turn"))
        path, beta, body = seen[0]
        wire = adapter.to_wire(request(TURN_B), EP)
        self.assertEqual((path, beta), ("/v1/messages", wire.pop("betas")[0]))
        self.assertEqual(body, {**wire, "stream": True})

    def test_error_event_mid_stream_is_status_0(self):
        error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        with self.assertRaises(LLMError) as caught:
            self.run_over(sse(self.START, error))
        self.assertEqual(caught.exception.status, 0)

    def test_read_error_mid_stream_is_status_0(self):
        async def cut():
            yield sse(self.START)
            raise httpx2.ReadError("connection reset")
        with self.assertRaises(LLMError) as caught:
            self.run_over(cut())
        self.assertEqual(caught.exception.status, 0)
        self.assertTrue(caught.exception.message.startswith("ReadError"))  # the SDK did not wrap it


if __name__ == "__main__":
    unittest.main()
