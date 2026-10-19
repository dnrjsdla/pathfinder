#!/usr/bin/env python3
"""fake_notion_mcp.py -- a stdio MCP server over fake_notion.World, for the partition-2 live check.
Serves the real better-notion tool specs (fixtures/notion_tools.json) verbatim. Usage:
    fake_notion_mcp.py WORLD_JSON      serve; seed WORLD_JSON from the default world if it is missing
    fake_notion_mcp.py --selfcheck     in-process round trip, prints PASS/FAIL
Every write persists to WORLD_JSON (pages, rows, `archived` = a list of page ids). Stdlib only."""
import json, os, sys, tempfile, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_notion
from fake_notion import World, dumps, norm_id

WRITES = {("pages", "create"), ("pages", "restore"), ("pages", "update"), ("pages", "archive")}
STATE = ("pages", "db", "rows", "cursor", "archived")


class CheckWorld(World):
    """fake_notion's world plus the two write actions the live check needs (create, restore)."""

    def _pages_create(self, a):
        i = str(uuid.uuid4())
        page = {"id": i, "object": "page", "title": a.get("title") or "Untitled", "url": "https://www.notion.so/" + i.replace("-", ""),
                "last_edited_time": "2026-10-19T09:00:00.000Z", "properties": a.get("properties") or {}}
        self.pages.append(page)
        return {"action": "create", "page_id": i, "url": page["url"], "properties": page["properties"]}

    def _pages_restore(self, a):
        p = self._find(a.get("page_id"))
        if not p or p["id"] not in self.archived:
            return {"error": "not found or not archived", "page_id": a.get("page_id")}
        self.archived.discard(p["id"])
        return {"action": "restore", "page_id": p["id"], "archived": False}

    def _pages_update(self, a):
        if a.get("archived") is False:
            return self._pages_restore(a)
        return super()._pages_update(a)


def save(w, path):
    state = {k: sorted(w.archived) if k == "archived" else getattr(w, k) for k in STATE}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(path):
    w = CheckWorld()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            w.__dict__.update(json.load(f))
        w.archived = set(w.archived)
    else:
        save(w, path)
    return w


def tool_result(w, path, name, args):
    """One tools/call. An unmodelled tool/action is an isError result, never an exception."""
    args = args if isinstance(args, dict) else {}
    if name not in ("workspace", "pages", "databases") or not hasattr(w, f"_{name}_{args.get('action')}"):
        out = {"error": f"unsupported in this fake: {name} {args.get('action')}"}
    else:
        out = w.call(name, args)
        if (name, args.get("action")) in WRITES and "error" not in out:
            save(w, path)
    return {"content": [{"type": "text", "text": dumps(out)}], "isError": "error" in out}


def handle(w, path, specs, msg):
    """One JSON-RPC message in, the reply out (None for a notification)."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-notion", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": specs}
    elif method == "tools/call":
        result = tool_result(w, path, params.get("name"), params.get("arguments"))
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": str(method)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def specs_of():
    with open(fake_notion.SPECS, encoding="utf-8") as f:
        return json.load(f)


def serve(path):
    w, specs = load(path), specs_of()
    for line in sys.stdin:
        if line.strip():
            reply = handle(w, path, specs, json.loads(line))
            if reply:
                sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def selfcheck():
    path = os.path.join(tempfile.mkdtemp(), "world.json")
    w, specs = load(path), specs_of()
    rpc = lambda method, **p: handle(w, path, specs, {"jsonrpc": "2.0", "id": 1, "method": method, "params": p})["result"]
    call = lambda name, **a: json.loads(rpc("tools/call", name=name, arguments=a)["content"][0]["text"])
    assert rpc("initialize")["serverInfo"]["name"] == "fake-notion"
    assert rpc("tools/list")["tools"] == specs and len(specs) == 11
    page = call("workspace", action="search", query="휴가 계획")["results"][0]
    assert call("pages", action="get", page_id=page["id"])["last_edited_time"] == page["last_edited_time"]
    assert call("pages", action="archive", page_id=page["id"])["processed"] == 1
    with open(path, encoding="utf-8") as f:
        assert page["id"] in json.load(f)["archived"]
    assert norm_id(page["id"]) in {norm_id(i) for i in load(path).archived}  # a restart still sees it
    assert call("pages", action="restore", page_id=page["id"])["archived"] is False
    call("pages", action="create", title="P2 check", properties={"Status": "Done"})
    assert load(path).pages[-1]["properties"] == {"Status": "Done"}
    bad = rpc("tools/call", name="blocks", arguments={"action": "get"})
    assert bad["isError"] is True and "unsupported" in json.loads(bad["content"][0]["text"])["error"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        try:
            selfcheck()
            print("PASS")
        except Exception as e:
            print("FAIL", repr(e))
            sys.exit(1)
    else:
        serve(sys.argv[1])
