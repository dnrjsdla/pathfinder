"""No network for the pure-parsing tests. `stream` is exercised against a
tiny local http.server bound to 127.0.0.1 on an OS-assigned port, running in
a background thread - loopback inside this test process, not an external
request."""
import contextlib
import http.server
import json
import threading
import unittest

from pathfinder.llm import Endpoint, LLMError, resolve
from pathfinder.llm.openai_compat import parse_sse, stream, _assemble


def _line(obj):
    return "data: " + json.dumps(obj)


@contextlib.contextmanager
def _server(body, status=200, content_type="text/event-stream"):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # drain the request body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # keep test output quiet

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()  # shutdown() only stops serve_forever; this frees the listening socket


class TestSSEParseAndAssembly(unittest.TestCase):
    def test_sse_parse_and_tool_call_assembly(self):
        chunks = [
            # call 0: no id (-> call_0), arguments arrive as a JSON string split across 3 deltas
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "get_weather"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cit'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'y": "Bus'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'an"}'}}]}}]},
            # call 1: real id, arguments arrive as an object in one delta
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_xyz",
                "function": {"name": "add_event", "arguments": {"title": "x"}}}]}}]},
            # call 2: no id (-> call_2), unparsable arguments -> {"_raw": ...}
            {"choices": [{"delta": {"tool_calls": [{"index": 2,
                "function": {"name": "bogus_tool", "arguments": "not json"}}]}}]},
        ]
        lines = [_line(c) for c in chunks] + ["data: [DONE]", "", "not-a-data-line"]

        parsed = list(parse_sse(lines))
        self.assertEqual(parsed, chunks)  # [DONE], blanks and non-data lines are dropped

        events = list(_assemble(parsed))
        self.assertEqual(len(events), 1)
        kind, tool_calls = events[0]
        self.assertEqual(kind, "tool_calls")
        self.assertEqual(tool_calls[0], {"id": "call_0", "name": "get_weather", "arguments": {"city": "Busan"}})
        self.assertEqual(tool_calls[1], {"id": "call_xyz", "name": "add_event", "arguments": {"title": "x"}})
        self.assertEqual(tool_calls[2], {"id": "call_2", "name": "bogus_tool", "arguments": {"_raw": "not json"}})

    def test_parse_sse_bytes_lines(self):
        lines = [b'data: {"choices": []}', b"data: [DONE]"]
        self.assertEqual(list(parse_sse(lines)), [{"choices": []}])


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.cfg = {"providers": {"ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": ""}}}

    def test_resolve_splits_on_first_colon(self):
        endpoint = resolve(self.cfg, "ollama:some-model:120b-tag")
        self.assertEqual(endpoint.model, "some-model:120b-tag")
        self.assertEqual(endpoint.base_url, "http://localhost:11434/v1")

    def test_resolve_empty_model_is_a_clear_error(self):
        with self.assertRaises(LLMError):
            resolve(self.cfg, "")
        with self.assertRaises(LLMError):
            resolve(self.cfg, "ollama:")  # provider given, model part empty

    def test_resolve_unknown_provider(self):
        with self.assertRaises(LLMError):
            resolve(self.cfg, "nonexistent-provider:some-model")


class TestStream(unittest.TestCase):
    def test_stream_end_to_end(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            b'data: [DONE]\n\n'
        )
        with _server(body) as base_url:
            endpoint = Endpoint(base_url=base_url, api_key="", model="fake-test-model")
            events = list(stream(endpoint, [{"role": "user", "content": "hi"}]))
        self.assertEqual(events[0], ("text", "Hello"))
        self.assertEqual(events[1], ("text", " world"))
        self.assertEqual(events[-1], ("usage", {"prompt_tokens": 5, "completion_tokens": 2}))

    def test_stream_blocked_on_402(self):
        err_body = json.dumps({"error": {"message": "insufficient credit"}}).encode("utf-8")
        with _server(err_body, status=402, content_type="application/json") as base_url:
            endpoint = Endpoint(base_url=base_url, api_key="", model="fake-test-model")
            with self.assertRaises(LLMError) as cm:
                list(stream(endpoint, [{"role": "user", "content": "hi"}]))
        self.assertEqual(cm.exception.status, 402)
        self.assertTrue(cm.exception.blocked)

    def test_stream_cancelled(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        with _server(body) as base_url:
            endpoint = Endpoint(base_url=base_url, api_key="", model="fake-test-model")
            cancel = threading.Event()
            gen = stream(endpoint, [{"role": "user", "content": "hi"}], cancel=cancel)
            self.assertEqual(next(gen), ("text", "Hello"))
            cancel.set()
            self.assertEqual(next(gen), ("cancelled", None))
            with self.assertRaises(StopIteration):
                next(gen)


class TestStreamFailuresAreLLMErrors(unittest.TestCase):
    def test_mid_stream_failures_become_llm_errors(self):
        """A read timeout, a reset and an incomplete read are not URLError. If they escape as
        themselves, a stalled model reads as an internal error instead of a blocked model."""
        import http.client
        from unittest import mock
        endpoint = Endpoint(base_url="http://127.0.0.1:9", api_key="", model="fake-model")
        for boom in (TimeoutError("timed out"), ConnectionResetError("reset"), http.client.IncompleteRead(b"")):
            with mock.patch("urllib.request.urlopen", side_effect=boom):
                with self.assertRaises(LLMError) as caught:
                    list(stream(endpoint, [{"role": "user", "content": "hi"}]))
            self.assertEqual(caught.exception.status, 0)


if __name__ == "__main__":
    unittest.main()
