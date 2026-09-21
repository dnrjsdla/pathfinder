"""Streaming chat completions against an OpenAI-compatible /chat/completions
endpoint (Ollama, and anything else that speaks the same wire format)."""
import http.client
import json
import urllib.error
import urllib.request

from . import LLMError


def parse_sse(lines):
    """Decode Server-Sent-Events `data: ...` lines into JSON chunks. Pure
    function (bytes or str lines in, no network) so tests can feed it canned
    lines directly. Stops at the `[DONE]` sentinel."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue  # a malformed line is dropped, not fatal


def _assemble(chunks, cancel=None):
    """Turn parsed SSE chunks into the (kind, payload) tuples `stream`
    yields. Split out from the network call so tool-call assembly can be
    tested against canned chunks, no server needed.

    `cancel` is checked between chunks (not mid-chunk): when set, stop and
    yield ("cancelled", None) instead of finishing the response.
    """
    tool_calls = {}  # index -> {"id", "name", "arguments"}
    usage = None
    for chunk in chunks:
        if cancel is not None and cancel.is_set():
            yield ("cancelled", None)
            return
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta", {}) or {}
            if delta.get("content"):
                yield ("text", delta["content"])
            reasoning = delta.get("reasoning") or delta.get("reasoning_content") or delta.get("thinking")
            if reasoning:
                yield ("reasoning", reasoning)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                entry = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    entry["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["name"] += fn["name"]
                args = fn.get("arguments")
                if isinstance(args, dict):
                    entry["arguments"] = args  # whole object arrived in one delta
                elif args:
                    # still a string: pieces accumulate as the server streams them
                    entry["arguments"] = (entry["arguments"] if isinstance(entry["arguments"], str) else "") + args
        if chunk.get("usage"):
            usage = chunk["usage"]

    if tool_calls:
        result = []
        for i, (_, entry) in enumerate(sorted(tool_calls.items())):
            args = entry["arguments"]
            if isinstance(args, dict):
                parsed = args
            else:
                try:
                    parsed = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": args}
            result.append({"id": entry["id"] or f"call_{i}", "name": entry["name"], "arguments": parsed})
        yield ("tool_calls", result)
    if usage:
        yield ("usage", usage)


def stream(endpoint, messages, tools=None, cancel=None, timeout=120):
    body = {"model": endpoint.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    req = urllib.request.Request(
        endpoint.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            yield from _assemble(parse_sse(resp), cancel=cancel)
    except urllib.error.HTTPError as e:
        with e:  # e is also the response body; read it before the socket closes
            message = e.read().decode("utf-8", errors="replace")
        raise LLMError(e.code, message[:300])
    except (OSError, http.client.HTTPException) as e:
        # URLError is an OSError; so are the failures that arrive MID-stream - a read timeout
        # (the timeout is per read, not total), a reset, an incomplete read. They must all be
        # LLMError, or a stalled model surfaces as "internal error" instead of a blocked model.
        raise LLMError(0, f"{type(e).__name__}: {e}"[:300])
