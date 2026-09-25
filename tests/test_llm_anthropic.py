"""Tests for the experimental Anthropic adapter (D7, D37) - recorded SSE fixtures only, no
network. Every TestCase here inherits `NoLiveCallTestCase`, which patches `urllib.request.urlopen`
to raise before any test body runs, so this suite can never reach api.anthropic.com even by
accident."""
import io
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

from pathfinder import llm
from pathfinder.llm import Endpoint, LLMError, anthropic
from pathfinder.llm.anthropic import _assemble, _convert_history, _convert_tools
from pathfinder.llm.openai_compat import parse_sse

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class NoLiveCallTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen",
                              side_effect=AssertionError("no live call allowed in this suite"))
        self.addCleanup(patcher.stop)
        patcher.start()


class TestTextStream(NoLiveCallTestCase):
    def test_text_stream_from_recorded_sse(self):
        events = list(_assemble(parse_sse(_load("anthropic_text.sse"))))
        text = "".join(p for k, p in events if k == "text")
        self.assertEqual(text, "Hello world")
        self.assertEqual([p for k, p in events if k == "usage"], [{"output_tokens": 5}])


class TestToolUse(NoLiveCallTestCase):
    def test_tool_use_assembled_from_input_json_deltas(self):
        events = list(_assemble(parse_sse(_load("anthropic_tool_use.sse"))))
        tool_calls = [p for k, p in events if k == "tool_calls"][0]
        self.assertEqual(tool_calls, [{"id": "toolu_000000000001", "name": "get_weather",
                                        "arguments": {"location": "Busan"}}])


class TestHistoryConversion(NoLiveCallTestCase):
    def test_history_conversion_round_trip(self):
        messages = [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "weather in Busan?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "toolu_000000000001", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"location": "Busan"}'}}]},
            {"role": "tool", "tool_call_id": "toolu_000000000001", "content": "22C, clear"},
            {"role": "user", "content": "thanks"},
        ]
        system, converted = _convert_history(messages)
        self.assertEqual(system, "Be terse.")
        self.assertEqual(converted[0],
                          {"role": "user", "content": [{"type": "text", "text": "weather in Busan?"}]})
        self.assertEqual(converted[1], {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_000000000001", "name": "get_weather",
             "input": {"location": "Busan"}}]})
        self.assertEqual(converted[2], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_000000000001", "content": "22C, clear"}]})
        self.assertEqual(converted[3], {"role": "user", "content": [{"type": "text", "text": "thanks"}]})

    def test_tools_converted_to_input_schema(self):
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "d",
            "parameters": {"type": "object", "properties": {"location": {"type": "string"}},
                            "required": ["location"]}}}]
        self.assertEqual(_convert_tools(tools), [{
            "name": "get_weather", "description": "d",
            "input_schema": {"type": "object", "properties": {"location": {"type": "string"}},
                              "required": ["location"]}}])


class TestHTTPErrors(NoLiveCallTestCase):
    def _http_error(self, status, body):
        return urllib.error.HTTPError(url="https://api.anthropic.com/v1/messages", code=status,
                                       msg="err", hdrs=None, fp=io.BytesIO(body))

    def test_429_is_blocked(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=self._http_error(429, b'{"error":{"message":"rate limited"}}')):
            endpoint = Endpoint(base_url="https://api.anthropic.com", api_key="k", model="m", api="anthropic")
            with self.assertRaises(LLMError) as cm:
                list(anthropic.stream(endpoint, [{"role": "user", "content": "hi"}]))
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(cm.exception.blocked)

    def test_402_is_blocked(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=self._http_error(402, b'{"error":{"message":"billing"}}')):
            endpoint = Endpoint(base_url="https://api.anthropic.com", api_key="k", model="m", api="anthropic")
            with self.assertRaises(LLMError) as cm:
                list(anthropic.stream(endpoint, [{"role": "user", "content": "hi"}]))
        self.assertEqual(cm.exception.status, 402)
        self.assertTrue(cm.exception.blocked)


class TestMidStreamError(NoLiveCallTestCase):
    def test_mid_stream_error_event(self):
        with self.assertRaises(LLMError) as cm:
            list(_assemble(parse_sse(_load("anthropic_error.sse"))))
        self.assertEqual(cm.exception.status, 529)


class TestDispatch(NoLiveCallTestCase):
    def test_dispatch_defaults_to_openai(self):
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": ""}}}
        endpoint = llm.resolve(cfg, "p:m")
        self.assertEqual(endpoint.api, "openai")
        with mock.patch("pathfinder.llm.openai_compat.stream",
                         return_value=iter([("text", "hi")])) as m_openai, \
             mock.patch("pathfinder.llm.anthropic.stream",
                         return_value=iter([("text", "no")])) as m_anthropic:
            list(llm.stream(endpoint, [{"role": "user", "content": "hi"}]))
        m_openai.assert_called_once()
        m_anthropic.assert_not_called()

    def test_unknown_api_is_a_config_error(self):
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": "", "api": "bogus"}}}
        with self.assertRaises(LLMError):
            llm.resolve(cfg, "p:m")


class TestNoLiveCallInTheSuite(unittest.TestCase):
    def test_no_live_call_in_the_suite(self):
        """Every test class above inherits NoLiveCallTestCase, which patches urlopen to raise
        before any test body runs (D7) - assert that guard is actually in place for each one."""
        module = sys.modules[__name__]
        for name, obj in vars(module).items():
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj not in (
                    NoLiveCallTestCase, TestNoLiveCallInTheSuite):
                self.assertTrue(issubclass(obj, NoLiveCallTestCase), f"{name} skips the urlopen guard")


if __name__ == "__main__":
    unittest.main()
