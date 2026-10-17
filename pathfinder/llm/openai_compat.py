"""Streaming chat completions against an OpenAI-compatible /chat/completions
endpoint (Ollama, and anything else that speaks the same wire format)."""
import asyncio
import http.client
import json
import urllib.error
import urllib.request

from . import LLMError, Response
from .rename import rename_args_out, rename_in, rename_out


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


def _post(endpoint, body, timeout=120):
    """POST `body` to /chat/completions and yield the parsed SSE chunks; every failure is an LLMError."""
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    req = urllib.request.Request(
        endpoint.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            yield from parse_sse(resp)
    except urllib.error.HTTPError as e:
        with e:  # e is also the response body; read it before the socket closes
            message = e.read().decode("utf-8", errors="replace")
        raise LLMError(e.code, message[:300])
    except (OSError, http.client.HTTPException) as e:
        # URLError is an OSError; so are the failures that arrive MID-stream - a read timeout
        # (the timeout is per read, not total), a reset, an incomplete read. They must all be
        # LLMError, or a stalled model surfaces as "internal error" instead of a blocked model.
        raise LLMError(0, f"{type(e).__name__}: {e}"[:300])


def stream(endpoint, messages, tools=None, cancel=None, timeout=120):
    body = {"model": endpoint.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    yield from _assemble(_post(endpoint, body, timeout), cancel=cancel)


def to_wire(req, endpoint):
    """The chat-completions body for `req`. Pure: the same Request gives the same JSON."""
    tools, renames = rename_out(req.tools)
    msgs, items = [{"role": "system", "content": req.system}], set()
    for m in req.messages:
        blocks = m["content"]
        if m["role"] == "system":  # the item's resident copy: once, in place
            if m["item"] not in items:
                items.add(m["item"])
                msgs.append({"role": "system", "content": blocks})
        elif m["role"] == "user":  # results as `tool` messages, then the user's text (steers)
            msgs += [{"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                     for b in blocks if b["type"] == "tool_result"]
            text = "\n\n".join(b["text"] for b in blocks if b["type"] == "text")
            if text:
                msgs.append({"role": "user", "content": text})
        else:  # thinking/reasoning blocks are never replayed
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(
                rename_args_out(b["name"], b["input"], renames), ensure_ascii=False)}}
                for b in blocks if b["type"] == "tool_use"]
            if text or calls:
                msgs.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
    body = {"model": endpoint.model, "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}, "max_tokens": req.max_tokens}
    if tools:
        body["tools"] = [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
                         for t in tools]
    return body


def _run(endpoint, req, emit, cancel):
    renames, finish = rename_out(req.tools)[1], []

    def chunks():  # remember finish_reason, which `_assemble` does not surface
        for c in _post(endpoint, to_wire(req, endpoint)):
            if c.get("error"):  # an in-stream error: let the router fall back
                raise LLMError(0, str(c["error"])[:300])
            finish.extend(ch["finish_reason"] for ch in c.get("choices") or [] if ch.get("finish_reason"))
            yield c

    text, thought, calls, usage, stop = [], [], [], {}, ""
    for kind, payload in _assemble(chunks(), cancel=cancel):
        if kind == "text":
            text.append(payload)
            emit(payload)
        elif kind == "reasoning":
            thought.append(payload)
        elif kind == "tool_calls":
            calls = payload
        elif kind == "usage":
            usage = payload
        else:
            stop = "interrupted"  # ("cancelled", None)
    blocks = ([{"type": "reasoning", "text": "".join(thought)}] if thought else []) \
        + ([{"type": "text", "text": "".join(text)}] if text else []) \
        + [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": rename_in(c["name"], c["arguments"], renames)}
           for c in calls]
    # a cut or filtered step wins over its tool calls: they are half-written and must not run
    stop = stop or {"length": "max_tokens", "content_filter": "refusal"}.get(
        finish[-1] if finish else "", "tool_use" if calls else "end_turn")
    tokens = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return Response(blocks, stop, tokens, endpoint.model, [])


async def complete(endpoint, req, on_text=None, cancel=None):
    """One model step. The blocking urllib stream runs in a thread; `on_text` is called on the event loop.
    On cancel the blocks come back as they arrived with stop_reason `interrupted`; the CLONE keeps only complete
    text blocks when it records or replays such a step (no reasoning, text marked partial). `Response.model` is
    the bare endpoint model; the ROUTER overwrites it with the `provider:model` ref before anyone records it."""
    loop = asyncio.get_running_loop()
    emit = (lambda s: loop.call_soon_threadsafe(  # no delta lands after an interrupt set `cancel`
        lambda: None if cancel is not None and cancel.is_set() else on_text(s))) if on_text else (lambda s: None)
    return await asyncio.to_thread(_run, endpoint, req, emit, cancel)


def embed(endpoint, texts):
    """One batched call to /embeddings (the OpenAI-compatible route Ollama also
    serves). Returns the vectors in input order, regardless of the order the
    server's `data` list comes back in."""
    body = {"model": endpoint.model, "input": texts}
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    req = urllib.request.Request(
        endpoint.base_url.rstrip("/") + "/embeddings",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in items]
    except urllib.error.HTTPError as e:
        with e:  # e is also the response body; read it before the socket closes
            message = e.read().decode("utf-8", errors="replace")
        raise LLMError(e.code, message[:300])
    except (OSError, http.client.HTTPException) as e:
        raise LLMError(0, f"{type(e).__name__}: {e}"[:300])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise LLMError(0, f"bad embeddings response: {type(e).__name__}: {e}"[:300])
