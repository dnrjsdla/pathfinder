"""No network for the pure-parsing tests. `complete` is exercised against a
tiny local http.server bound to 127.0.0.1 on an OS-assigned port, running in
a background thread - loopback inside this test process, not an external
request."""
import asyncio
import contextlib
import copy
import http.server
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from pathfinder.llm import Endpoint, LLMError, Request, Response, resolve
from pathfinder.llm.openai_compat import complete, embed, parse_sse, to_wire, _assemble
from pathfinder.llm.rename import rename_args_out, rename_in, rename_out

NOTION = Path(__file__).resolve().parent.parent / "bench" / "v2" / "fixtures" / "notion_tools.json"


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
        patcher = mock.patch("pathfinder.keychain.secret", return_value=None)  # never the real Keychain
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_unknown_api_is_a_config_error(self):
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": "", "api": "bogus"}}}
        with self.assertRaises(LLMError):
            resolve(cfg, "p:m")

    def test_resolve_carries_effort(self):
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": "FAKE_PF_KEY", "api": "anthropic",
                                   "effort": "xhigh"}, "q": {"base_url": "http://y", "api_key_env": ""}}}
        with mock.patch.dict("os.environ", {"FAKE_PF_KEY": "from-env"}):
            self.assertEqual(resolve(cfg, "p:m").effort, "xhigh")
        self.assertEqual(resolve(cfg, "q:m").effort, "")

    def test_resolve_key_from_env_then_keychain(self):
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": "FAKE_PF_KEY", "api": "anthropic"}}}
        with mock.patch.dict("os.environ", {"FAKE_PF_KEY": "from-env"}), \
             mock.patch("pathfinder.keychain.secret", return_value="from-keychain") as secret:
            self.assertEqual(resolve(cfg, "p:m").api_key, "from-env")
            secret.assert_not_called()
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("pathfinder.keychain.secret", return_value="from-keychain") as secret:
            self.assertEqual(resolve(cfg, "p:m").api_key, "from-keychain")
            secret.assert_called_once_with("pathfinder-p", "api")


class TestResolveKeychain(unittest.TestCase):
    def test_resolve_openai_provider_never_asks_the_keychain(self):
        cfg = {"providers": {"ollama": {"base_url": "http://x", "api_key_env": ""}}}
        with mock.patch("subprocess.run", side_effect=AssertionError("security was called")):
            self.assertEqual(resolve(cfg, "ollama:m").api_key, "")

    def test_resolve_keychain_through_security_binary(self):
        """The unpatched `keychain.secret` with only `subprocess.run` faked: service pathfinder-<provider>."""
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="fake-key\n", stderr="")

        cfg = {"providers": {"p": {"base_url": "http://x", "api_key_env": "", "api": "anthropic"}}}
        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(resolve(cfg, "p:m").api_key, "fake-key")
        self.assertEqual(calls[0][:6], ["security", "find-generic-password", "-s", "pathfinder-p", "-a", "api"])


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
                    asyncio.run(complete(endpoint, Request("root", "S")))
            self.assertEqual(caught.exception.status, 0)


class TestEmbedFailuresAreLLMErrors(unittest.TestCase):
    """memory and search fall back on LLMError alone: nothing else may escape `embed`."""

    def _embed(self, url):
        return embed(Endpoint(base_url=url, api_key="", model="fake-model"), ["x"])

    def test_a_base_url_without_a_scheme(self):
        with self.assertRaises(LLMError):
            self._embed("localhost:9/v1")

    def test_a_body_that_is_not_utf8(self):
        with _server(b"\xff\xfe{", content_type="application/json") as url, self.assertRaises(LLMError):
            self._embed(url)

    def test_an_embedding_that_is_not_a_list_of_numbers(self):
        for bad in (None, "0.1 0.2", ["0.1"], [True]):
            body = json.dumps({"data": [{"index": 0, "embedding": bad}]}).encode()
            with self.subTest(bad=bad), _server(body, content_type="application/json") as url:
                with self.assertRaises(LLMError):
                    self._embed(url)
        body = json.dumps({"data": [{"index": 0, "embedding": [0.5, 1]}]}).encode()
        with _server(body, content_type="application/json") as url:
            self.assertEqual(self._embed(url), [[0.5, 1]])


def _neutral(spec):
    return {"name": spec["name"], "description": spec["description"], "parameters": spec["inputSchema"]}


def _notion(*names):
    return [_neutral(t) for t in json.loads(NOTION.read_text()) if t["name"] in names]


def _tool(props):
    return {"name": "t", "description": "d", "parameters": {"type": "object", "properties": props}}


class TestV21Rename(unittest.TestCase):
    def test_v21_rename_round_trip(self):
        """Real Notion specs: only the keys named properties/description inside a properties map change,
        and the model's arguments come back under the original names."""
        tools = _notion("pages", "databases")
        out, renames = rename_out(tools)
        for before, after in zip(tools, out):
            props, new = before["parameters"]["properties"], after["parameters"]["properties"]
            self.assertEqual(before["description"], after["description"])  # prose is not touched
            self.assertEqual(list(new), [k + "_" if k in ("properties", "description") else k for k in props])
            self.assertEqual({k: v for k, v in props.items() if k not in ("properties", "description")},
                             {k: v for k, v in new.items() if k not in ("properties_", "description_")})
            self.assertEqual(new["properties_"], props["properties"])
            self.assertIn("properties", props)  # the input is not mutated
        self.assertEqual(renames, {"pages": {(): {"properties": "properties_"}},
                                   "databases": {(): {"properties": "properties_", "description": "description_"}}})
        shown = {"action": "update", "page_id": "p1", "properties_": {"Name": "x"}}
        original = {"action": "update", "page_id": "p1", "properties": {"Name": "x"}}
        self.assertEqual(rename_in("pages", shown, renames), original)
        self.assertEqual(rename_args_out("pages", original, renames), shown)

    def test_collision_and_untouched_tools(self):
        out, renames = rename_out([_tool({"properties": {}, "properties_": {}, "x": {}}), _tool({"x": {}})])
        self.assertEqual(list(out[0]["parameters"]["properties"]), ["properties__", "properties_", "x"])
        self.assertEqual(renames, {"t": {(): {"properties": "properties__"}}})
        self.assertEqual(out[1], _tool({"x": {}}))
        self.assertEqual(rename_out([_tool({"x": {}})]), ([_tool({"x": {}})], {}))
        self.assertEqual(rename_in("t", {"properties__": 1, "properties_": 2}, renames), {"properties": 1, "properties_": 2})

    def test_nested_properties_and_items(self):
        nested = _tool({"properties": {"type": "object", "properties": {"description": {"type": "string"}}},
                        "rows": {"type": "array", "items": {"type": "object", "properties": {"description": {}}}}})
        out, renames = rename_out([nested])
        props = out[0]["parameters"]["properties"]
        self.assertEqual(props["properties_"]["properties"], {"description_": {"type": "string"}})
        self.assertEqual(props["rows"]["items"]["properties"], {"description_": {}})
        shown = {"properties_": {"description_": "a"}, "rows": [{"description_": "b"}, {"other": 1}]}
        original = {"properties": {"description": "a"}, "rows": [{"description": "b"}, {"other": 1}]}
        self.assertEqual(rename_in("t", shown, renames), original)
        self.assertEqual(rename_args_out("t", original, renames), shown)

    def test_required_follows_the_rename(self):
        tool = _tool({"description": {}, "x": {}})
        tool["parameters"]["required"] = ["description", "x"]
        out, _ = rename_out([tool])
        self.assertEqual(out[0]["parameters"]["required"], ["description_", "x"])
        self.assertEqual(tool["parameters"]["required"], ["description", "x"])

    def test_array_of_arrays_maps_back(self):
        tool = _tool({"rows": {"type": "array", "items": {"type": "array", "items": {
            "type": "object", "properties": {"description": {}}}}}})
        _, renames = rename_out([tool])
        self.assertEqual(rename_in("t", {"rows": [[{"description_": 1}]]}, renames), {"rows": [[{"description": 1}]]})
        self.assertEqual(rename_args_out("t", {"rows": [[{"description": 1}]]}, renames),
                         {"rows": [[{"description_": 1}]]})

    def test_both_names_renamed_wins_original_only_kept(self):
        _, renames = rename_out([_tool({"description": {}})])
        self.assertEqual(rename_in("t", {"description": "old", "description_": "new"}, renames), {"description": "new"})
        self.assertEqual(rename_in("t", {"description": "kept"}, renames), {"description": "kept"})
        self.assertEqual(rename_in("other", {"description_": 1}, renames), {"description_": 1})


def _request():
    history = (
        {"role": "user", "content": [{"type": "text", "text": "find the page"}]},
        {"role": "system", "content": "RESIDENT", "item": "t1"},
        {"role": "assistant", "model": "p:m", "content": [
            {"type": "reasoning", "text": "hmm"}, {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "c1", "name": "pages", "input": {"action": "update", "properties": {"a": 1}}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"ok": true}'},
                                     {"type": "text", "text": "also: be quick"}]},
        {"role": "system", "content": "RESIDENT", "item": "t1"},
    )
    return Request("root", "SYSTEM", tuple(_notion("pages")), history)


class TestToWire(unittest.TestCase):
    endpoint = Endpoint(base_url="http://x", api_key="", model="fake-model")

    def test_to_wire_is_stable_and_shaped(self):
        req = _request()
        before = copy.deepcopy(req)
        body = to_wire(req, self.endpoint)
        self.assertEqual(json.dumps(body), json.dumps(to_wire(_request(), self.endpoint)))
        self.assertEqual(req, before)  # the Request is not mutated
        self.assertEqual((body["model"], body["stream"], body["max_tokens"]), ("fake-model", True, 16000))
        self.assertNotIn("tool_choice", body)
        self.assertEqual([(m["role"], m.get("content")) for m in body["messages"]], [
            ("system", "SYSTEM"), ("user", "find the page"), ("system", "RESIDENT"), ("assistant", "looking"),
            ("tool", '{"ok": true}'), ("user", "also: be quick")])  # one resident copy per item; no thinking
        call = body["messages"][3]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "pages")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"action": "update", "properties_": {"a": 1}})
        self.assertEqual(body["messages"][4]["tool_call_id"], "c1")
        props = body["tools"][0]["function"]["parameters"]["properties"]
        self.assertIn("properties_", props)
        self.assertNotIn("properties", props)
        self.assertNotIn("tools", to_wire(Request("root", "S"), self.endpoint))


class TestComplete(unittest.TestCase):
    def _run(self, body, req=None, status=200, **kw):
        with _server(body, status=status) as base_url:
            endpoint = Endpoint(base_url=base_url, api_key="", model="fake-test-model")
            return asyncio.run(complete(endpoint, req or Request("root", "S"), **kw))

    def test_complete_text_usage_and_on_text(self):
        body = (b'data: {"choices":[{"delta":{"reasoning":"think"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
                b'data: [DONE]\n\n')
        seen = []
        resp = self._run(body, on_text=seen.append)
        self.assertEqual(seen, ["Hello", " world"])
        self.assertEqual(resp, Response(
            [{"type": "reasoning", "text": "think"}, {"type": "text", "text": "Hello world"}], "end_turn",
            {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "fake-test-model", []))

    def test_complete_tool_call_is_renamed_back(self):
        args = json.dumps({"action": "update", "properties_": {"a": 1}})
        chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "pages", "arguments": args}}]},
                              "finish_reason": "tool_calls"}]}
        resp = self._run(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
                         Request("root", "S", tuple(_notion("pages"))))
        self.assertEqual(resp.stop_reason, "tool_use")
        self.assertEqual(resp.blocks, [{"type": "tool_use", "id": "c9", "name": "pages",
                                        "input": {"action": "update", "properties": {"a": 1}}}])

    def test_complete_stop_reasons_cancel_and_errors(self):
        length = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n'
        self.assertEqual(self._run(length).stop_reason, "max_tokens")
        cut = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "pages", "arguments": '{"action": "upd'}}]}, "finish_reason": "length"}]}
        resp = self._run(("data: " + json.dumps(cut) + "\n\ndata: [DONE]\n\n").encode())
        self.assertEqual(resp.stop_reason, "max_tokens")  # a tool call cut at the limit is not tool_use
        self.assertEqual(resp.blocks[0]["input"], {"_raw": '{"action": "upd'})
        filtered = b'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\ndata: [DONE]\n\n'
        self.assertEqual(self._run(filtered).stop_reason, "refusal")
        with self.assertRaises(LLMError) as cm:
            self._run(b'data: {"error": {"message": "overloaded"}}\n\ndata: [DONE]\n\n')
        self.assertEqual(cm.exception.status, 0)
        self.assertIn("overloaded", cm.exception.message)
        cancel = threading.Event()
        cancel.set()
        resp = self._run(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n', cancel=cancel)
        self.assertEqual((resp.stop_reason, resp.blocks), ("interrupted", []))
        with self.assertRaises(LLMError) as cm:
            self._run(b'{"error": "no credit"}', status=402)
        self.assertTrue(cm.exception.blocked)


if __name__ == "__main__":
    unittest.main()
