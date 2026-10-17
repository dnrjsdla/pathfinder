"""
m2_stub.py -- no-network fake Anthropic client, the --selfcheck, the scorer and the key lookup for m2_resume.py.
The fake binds each thinking block's signature to the prefix before it (thinking excluded, so a
stripped leading run stays valid, as the docs say); a mismatch -> HTTP 400, or with
prefix_mismatch_behavior=drop_block -> reported in input_transformations.
"""
import hashlib, json, os, re, subprocess, uuid
from types import SimpleNamespace

import anthropic
import httpx2  # the HTTP client anthropic 1.15.0 is built on

_THINK = ("thinking", "redacted_thinking")


def _canon(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sig(kw, prefix, text):
    bare = [{**m, "content": [b for b in m["content"] if b["type"] not in _THINK]} for m in prefix]
    return hashlib.sha256((_canon([kw["system"], kw["tools"], bare]) + text).encode()).hexdigest()[:24]


class StubClient:
    """Scripted model: prompt -> search_pages; search result -> archive_page; approval -> done text."""

    def __init__(self):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, betas=None, **kw):
        self.calls.append({"betas": betas, **kw})
        msgs, dropped = kw["messages"], []
        drop = kw["thinking"].get("block_binding", {}).get("prefix_mismatch_behavior") == "drop_block"
        for i, m in enumerate(msgs):
            for j, b in enumerate(m["content"]):
                if b["type"] == "thinking" and b["signature"] != _sig(kw, msgs[:i], b["thinking"]):
                    if not drop:
                        raise anthropic.BadRequestError(
                            "Invalid signature in thinking block. The block is bound to a different conversation",
                            body=None, response=httpx2.Response(400, request=httpx2.Request("POST", "https://stub.invalid")))
                    dropped.append({"type": "thinking_dropped", "reason": "prefix_binding_mismatch",
                                    "path": f"messages[{i}].content[{j}]"})
        title = re.search(r"'(.+)'", msgs[0]["content"][0]["text"]).group(1)
        last = msgs[-1]["content"][-1]
        data = json.loads(last["content"]) if last["type"] == "tool_result" else None

        def use(name, inp):
            return {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": name, "input": inp}

        if data is None:
            tail = use("search_pages", {"query": title})
        elif isinstance(data, list):
            tail = use("archive_page", {"page_id": next(p["id"] for p in data if p["title"] == title)})
        else:
            tail = {"type": "text", "text": "Done: the page is archived."}
        think = "Next step for " + title
        body = {"id": "msg_stub", "type": "message", "role": "assistant", "model": kw["model"], "stop_sequence": None,
                "content": [{"type": "thinking", "thinking": think, "signature": _sig(kw, msgs, think)}, tail],
                "stop_reason": "tool_use" if tail["type"] == "tool_use" else "end_turn",
                "usage": {"input_tokens": len(_canon(msgs)) // 4, "output_tokens": 50}}
        if betas is None:
            return anthropic.types.Message.model_validate(body)
        return anthropic.types.beta.BetaMessage.model_validate({**body, "input_transformations": dropped})


def summarize(records, arms, trials):
    out = {}
    for a in arms:
        rs = [r for r in records if r["arm"] == a]
        s = {"trials": len(rs), "resumed": sum(r["resumed"] for r in rs), "http_400": sum(r["status"] == 400 for r in rs),
             "prefix_identical": sum(bool(r["prefix_identical"]) for r in rs)}
        tk = [r["thinking_blocks_pre_gate"] or 0 for r in rs]
        s["thinking_blocks_pre_gate"] = [min(tk, default=0), max(tk, default=0)]
        ok = s["resumed"] == trials and s["http_400"] == 0
        s["verdict"] = "untested" if ok and not any(tk) else "pass" if ok else "fail"  # no thinking -> nothing to strip
        out[a] = {**s, "pass": s["verdict"] == "pass"}
    return {"arms": out, "passing_arms": [a for a, s in out.items() if s["pass"]],
            "untested_arms": [a for a, s in out.items() if s["verdict"] == "untested"]}


def api_key():
    try:
        return os.environ.get("ANTHROPIC_API_KEY") or subprocess.run(
            ["security", "find-generic-password", "-s", "pathfinder-anthropic", "-w"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def selfcheck(m2):
    """m2 = the m2_resume module. Runs every arm on the stub and checks ledger, prefix, 400 path, scorer."""
    stub, recs = StubClient(), []
    want_think = {"replay": [1, 1], "strip-all": [0, 0], "strip-all-drop": [0, 0], "strip-last-only": [1, 0]}
    for arm in m2.ARMS:
        for i in range(2):
            r, req = m2.run_trial(stub, "stub-model", arm, i, "high" if i else None), stub.calls[-1]
            assert r["status"] == 200 and r["resumed"] and r["prefix_identical"] and r["requests"] == 3, r
            assert r["thinking_blocks_pre_gate"] == 2 and r["thinking_blocks_sent_on_resume"] == sum(want_think[arm]), r
            assert req.get("output_config") == ({"effort": "high"} if i else None), req.get("output_config")
            assert not r["extra_search"] and r["tokens"]["in"] > 0 and not r["input_transformations"], r
            n = [sum(b["type"] == "thinking" for b in x["content"]) for x in req["messages"] if x["role"] == "assistant"]
            assert n == want_think[arm] and (req["betas"] == [m2.BETA]) == (arm == "strip-all-drop"), (arm, n)
            recs.append(r)
    # the prefix check must flag a removal the arm did not ask for
    full = json.loads(m2.canon(stub.calls[-1]["messages"][:-1]))  # pre-gate history, thinking kept in msg 1
    sent = m2.canon({"system": m2.SYSTEM, "tools": m2.TOOLS, "messages": full})  # key order kept, as the real check
    rebuilt = m2.strip(full, "strip-all") + [{}]
    assert m2.prefix_identical(sent, "strip-all", m2.SYSTEM, m2.TOOLS, rebuilt, len(full))
    assert not m2.prefix_identical(sent, "replay", m2.SYSTEM, m2.TOOLS, rebuilt, len(full))
    # a mid-prefix edit breaks the binding: 400 plain, dropped + reported with drop_block
    bad = json.loads(_canon(stub.calls[-1]["messages"]))
    bad[0]["content"][0]["text"] += " (edited)"
    st, _, err = m2.send(stub, "replay", "stub-model", m2.SYSTEM, m2.TOOLS, bad)
    assert st == 400 and "Invalid signature" in err, (st, err)
    st, resp, _ = m2.send(stub, "strip-all-drop", "stub-model", m2.SYSTEM, m2.TOOLS, bad)
    assert st == 200 and m2.jsonable(resp.input_transformations)[0]["type"] == "thinking_dropped", st
    # scorer: one 400 fails its arm only
    assert m2.summarize(recs, m2.ARMS, 2)["passing_arms"] == m2.ARMS
    recs[0] = dict(recs[0], status=400, resumed=False)
    assert m2.summarize(recs, m2.ARMS, 2)["passing_arms"] == m2.ARMS[1:]
    # scorer: an arm that never saw a thinking block before the gate is untested, not passed
    recs = [dict(r, thinking_blocks_pre_gate=0) if r["arm"] == "replay" else r for r in recs]
    sm = m2.summarize(recs, m2.ARMS, 2)
    assert sm["untested_arms"] == ["replay"] and sm["arms"]["replay"]["verdict"] == "untested", sm
    assert sm["passing_arms"] == ["strip-all-drop", "strip-last-only"] and sm["arms"]["strip-all"]["verdict"] == "fail", sm
    print(f"selfcheck ok: {len(stub.calls)} stub requests, 4 arms, ledger round trip, prefix check, 400, drop_block, effort, thinking counts, untested")
