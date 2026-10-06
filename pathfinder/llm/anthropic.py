"""Anthropic Messages API adapter - EXPERIMENTAL, never run live (D7): no paid key is
available, so this module is built to spec and unit-tested against recorded SSE fixtures only
(D37). It exists so D7's reversal (a key appears) is a config change, not a rewrite - a provider
entry sets `providers.<name>.api = "anthropic"` (still experimental until that day) and `llm.stream`
picks this module by `endpoint.api`.

`stream()` yields the same (kind, payload) tuples `openai_compat.stream` yields, so the loop and
the slot never know which adapter answered."""
import http.client
import json
import urllib.error
import urllib.request

from . import LLMError
from .openai_compat import parse_sse

ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 4096  # module constant; no per-call override requested


def _convert_history(messages):
    """OpenAI-shaped history (as the loop keeps it) -> (system_string_or_None, anthropic_messages).
    `system` messages join into the top-level `system` string; assistant `tool_calls` become
    `tool_use` blocks after any text block; consecutive `tool` messages group into ONE user turn
    of `tool_result` blocks; plain user/assistant text becomes a text block."""
    system_parts = []
    out = []
    last_was_tool_group = False
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            if msg.get("content"):
                system_parts.append(msg["content"])
            last_was_tool_group = False
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"],
                     "content": msg.get("content", "")}
            if last_was_tool_group:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
                last_was_tool_group = True
            continue
        last_was_tool_group = False
        if role == "assistant":
            content = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                fn = tc["function"]
                content.append({"type": "tool_use", "id": tc["id"], "name": fn["name"],
                                 "input": json.loads(fn["arguments"] or "{}")})
            out.append({"role": "assistant", "content": content})
        else:  # user (or anything else) -> plain text
            out.append({"role": "user", "content": [{"type": "text", "text": msg.get("content", "")}]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


def _convert_tools(tools):
    """OpenAI `{"type": "function", "function": {name, description, parameters}}` -> Anthropic
    `{name, description, input_schema}`."""
    if not tools:
        return None
    return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})}
            for t in tools]


def _assemble(chunks, cancel=None):
    """Parsed SSE event dicts -> the (kind, payload) tuples `stream` yields. Split out from the
    network call so it can be tested against canned events, no server needed. `cancel` is
    checked between chunks, like openai_compat._assemble."""
    tool_uses = {}  # content-block index -> {"id", "name", "json"}
    usage = None
    for chunk in chunks:
        if cancel is not None and cancel.is_set():
            yield ("cancelled", None)
            return
        kind = chunk.get("type")
        if kind == "content_block_start":
            block = chunk.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_uses[chunk.get("index", 0)] = {"id": block.get("id", ""),
                                                     "name": block.get("name", ""), "json": ""}
        elif kind == "content_block_delta":
            delta = chunk.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield ("text", delta.get("text", ""))
            elif dtype == "input_json_delta":
                entry = tool_uses.get(chunk.get("index", 0))
                if entry is not None:
                    entry["json"] += delta.get("partial_json", "")
        elif kind == "message_delta":
            if chunk.get("usage"):
                usage = chunk["usage"]  # cumulative, not incremental - the last one wins
        elif kind == "error":
            err = chunk.get("error") or {}
            status = {"overloaded_error": 529, "rate_limit_error": 429}.get(err.get("type"), 0)
            raise LLMError(status, (err.get("message") or "")[:300])
        # content_block_stop, message_start, message_stop, ping: nothing to assemble

    if tool_uses:
        result = []
        for idx in sorted(tool_uses):
            entry = tool_uses[idx]
            try:
                parsed = json.loads(entry["json"]) if entry["json"] else {}
            except json.JSONDecodeError:
                parsed = {"_raw": entry["json"]}
            result.append({"id": entry["id"], "name": entry["name"], "arguments": parsed})
        yield ("tool_calls", result)
    if usage:
        yield ("usage", usage)


def stream(endpoint, messages, tools=None, cancel=None, timeout=120):
    # experimental (D7): reached only when a provider config sets api = "anthropic", which no
    # shipped config does today - never call this against a live api.anthropic.com key.
    system, converted = _convert_history(messages)
    body = {"model": endpoint.model, "max_tokens": MAX_TOKENS, "messages": converted, "stream": True}
    if system:
        body["system"] = system
    converted_tools = _convert_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if endpoint.api_key:
        headers["x-api-key"] = endpoint.api_key
    req = urllib.request.Request(
        endpoint.base_url.rstrip("/") + "/v1/messages",
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
        raise LLMError(0, f"{type(e).__name__}: {e}"[:300])
