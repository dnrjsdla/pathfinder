"""Anthropic Messages API adapter on the official SDK (V13). `to_wire` turns a neutral Request into the SDK's
keyword arguments (pure, byte-stable); `complete` streams one model step. The SDK is imported lazily, so the
package imports without it: only an `api: "anthropic"` provider needs it installed."""
import asyncio
import functools

from . import THINK_TYPES, LLMError, Response

CLEAR_AT_BETA = "mid-conversation-system-clear-at-2026-08-21"
# Deviation 5. True: a resident copy after every user message of an item, each cleared at the next user message
# (beta). False, the fallback: one copy per item, after its opening user message, kept in place (no beta).
RESIDENT_CLEAR_AT = True
THINKING = {"type": "adaptive", "display": "summarized"}  # summarized: same cost, P4 reads the text
BREAKPOINT = {"type": "ephemeral"}
STOP_REASONS = {"stop_sequence": "end_turn", "model_context_window_exceeded": "max_tokens"}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _marked(block):
    return {**block, "cache_control": BREAKPOINT}


def to_wire(req, endpoint):
    """The SDK keyword arguments for `req`. Pure: the same Request gives the same JSON. Three breakpoints: the
    last tool, the static system, and the last block of the last user message (the block before the trailing
    resident copy; never on a `clear_at` message, which is a 400). Assistant blocks are replayed verbatim; thinking
    is dropped from a message another model wrote. History is append-only: only that last breakpoint moves."""
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
             for t in req.tools]
    if tools:
        tools[-1] = _marked(tools[-1])
    msgs, items = [], set()
    for m in req.messages:
        if m["role"] == "system":  # the item's resident copy
            if RESIDENT_CLEAR_AT:
                msgs.append({"role": "system", "content": m["content"], "clear_at": "next_user_message"})
            elif m["item"] not in items:
                items.add(m["item"])
                msgs.append({"role": "system", "content": m["content"]})
        elif m["role"] == "user":  # text and tool_result blocks, already one message
            msgs.append({"role": "user", "content": list(m["content"])})
        else:
            same = m["model"].partition(":")[2] == endpoint.model
            content = [b for b in m["content"] if same or b["type"] not in THINK_TYPES]
            if content:  # a message that held only dropped thinking is skipped (as openai_compat does)
                msgs.append({"role": "assistant", "content": content})
    # a system message must be followed by an assistant turn or end the list: a skipped assistant can leave a
    # resident copy right before a user message (a 400), so that copy goes too
    msgs = [m for m, after in zip(msgs, msgs[1:] + [None])
            if m["role"] != "system" or after is None or after["role"] != "user"]
    users = [m for m in msgs if m["role"] == "user"]
    if users:
        users[-1]["content"][-1] = _marked(users[-1]["content"][-1])
    body ={"model": endpoint.model, "max_tokens": req.max_tokens,
            "system": [_marked({"type": "text", "text": req.system})], "messages": msgs, "thinking": THINKING}
    if tools:
        body["tools"] = tools
    if endpoint.effort:
        body["output_config"] = {"effort": endpoint.effort}
    if any("clear_at" in m for m in msgs):
        body["betas"] = [CLEAR_AT_BETA]
    return body


@functools.cache
def _client(base_url, api_key):
    import anthropic
    return anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)


def _response(msg, stop_reason, endpoint):
    blocks = [b.model_dump(exclude_none=True) for b in msg.content] if msg else []
    usage = {k: (getattr(msg.usage, k, 0) or 0) if msg else 0 for k in USAGE_KEYS}
    refusal = stop_reason == "refusal" and msg.stop_details
    details = msg.stop_details.model_dump(exclude_none=True) if refusal else None
    return Response(blocks, stop_reason, usage, endpoint.model, [], details)


async def complete(endpoint, req, on_text=None):
    """One streamed model step; `on_text` gets each text delta on the event loop. Cancelled by cancelling its
    task: the blocks come back as they arrived with stop_reason `interrupted` (the CLONE keeps only complete text
    blocks). `Response.model` is the bare endpoint model; the ROUTER overwrites it with the `provider:model` ref.
    SDK errors become `LLMError(status, message)`; nothing is retried here beyond the SDK's own retries."""
    import anthropic
    import httpx2
    body = to_wire(req, endpoint)
    client = _client(endpoint.base_url, endpoint.api_key)
    api = client.beta.messages if "betas" in body else client.messages
    msg = None
    try:
        async with api.stream(**body) as stream:
            async for event in stream:
                msg = stream.current_message_snapshot
                if event.type == "text" and on_text:
                    on_text(event.text)
            msg = await stream.get_final_message()
    except asyncio.CancelledError:
        return _response(msg, "interrupted", endpoint)
    except anthropic.APIStatusError as e:  # 200 = an `error` event mid-stream: report it as a connection failure
        raise LLMError(0 if e.status_code == 200 else e.status_code, str(e.message)[:300])
    except (anthropic.APIError, httpx2.HTTPError) as e:  # connection, timeout, a read cut mid-stream: no status
        raise LLMError(0, f"{type(e).__name__}: {e}"[:300])
    return _response(msg, STOP_REASONS.get(msg.stop_reason, msg.stop_reason), endpoint)
