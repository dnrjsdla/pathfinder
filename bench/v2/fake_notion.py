#!/usr/bin/env python3
"""fake_notion.py -- synthetic Notion world for bench/v2/m1_read_result.py (v2 M1). The model sees
the REAL better-notion-mcp specs for workspace/pages/databases (notion_specs.json, from tools/list);
handlers fake only what N1-N10 need, plus the store arm's read_result. Synthetic, deterministic. Stdlib."""
import json, os, random, re, uuid
from datetime import datetime, timedelta, timezone

NAMED = ["휴가 계획", "강의 노트", "프로젝트 계획", "독서 목록", "주간 플래너 템플릿",
         "월간 플래너 템플릿", "연간 플래너 템플릿", "운동 기록", "레시피 모음",
         "여행 체크리스트", "회의록", "가계부", "아이디어 메모"]
N_UNTITLED = 86          # 13 named + 86 Untitled + 1 database = 100 search objects
DB_TITLE = "강의 노트 DB"
LONG_TITLE = "강의 노트"   # the page whose content comes in two chunks (N4)
LONG = ("\n".join(f"## {k}주차\n- 핵심 개념 {k}-1 정리\n- 핵심 개념 {k}-2 정리\n- 과제: 연습문제 {k}장"
                  for k in range(1, 9)),
        "\n".join(f"## {k}주차\n- 핵심 개념 {k}-1 정리\n- 과제: 연습문제 {k}장"
                  for k in range(9, 16)) + "\n## 마지막 메모\n- 기말고사 범위: 9~15주차")
SPECS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "notion_tools.json")


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def norm_id(s):
    return str(s or "").replace("-", "").lower()


def notion_tools():
    """Real specs in OpenAI function shape (names, descriptions, schemas unchanged)."""
    with open(SPECS, encoding="utf-8") as f:
        specs = json.load(f)
    return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
             "parameters": s["inputSchema"]}} for s in specs if s["name"] in ("workspace", "pages", "databases")]


def items_of(r):
    """The list inside a result: a bare list, or a dict's `results` list; else None."""
    if isinstance(r, list):
        return r
    if isinstance(r, dict) and isinstance(r.get("results"), list):
        return r["results"]
    return None


def cut(obj, limit):
    """Compact JSON of at most `limit` chars, cut at a list-element boundary with a note."""
    s = dumps(obj)
    items = items_of(obj)
    if len(s) <= limit:
        return s
    if items is None:
        # ponytail: non-list results are cut as plain text; add per-field cuts if a big object shows up
        return s[:limit - 9] + "...(cut)"
    n = len(items)
    for k in range(n - 1, -1, -1):  # ponytail: linear shrink, n <= 100 here
        note = {"note": f"shown {k} of {n} results"}
        out = items[:k] + [note] if isinstance(obj, list) else {**obj, "results": items[:k], **note}
        s = dumps(out)
        if len(s) <= limit:
            return s
    return dumps({"note": f"shown 0 of {n} results"})


class World:
    def __init__(self, seed=0, fail_first_archive=False):
        rng = random.Random(seed)
        base = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)

        def obj(title, kind="page"):
            i = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            t = base - timedelta(minutes=rng.randint(0, 60 * 24 * 90))
            return {"id": i, "object": kind, "title": title,
                    "url": "https://www.notion.so/" + i.replace("-", ""),
                    "last_edited_time": t.strftime("%Y-%m-%dT%H:%M:%S.000Z")}

        self.pages = [obj(t) for t in NAMED] + [obj("Untitled") for _ in range(N_UNTITLED)]
        rng.shuffle(self.pages)
        self.db = obj(DB_TITLE, "data_source")
        self.rows = [dict(obj(f"{k}주차 강의"), status="Done" if k <= 3 else
                          ("In progress" if k % 2 else "Not started")) for k in range(1, 9)]
        self.by_title = {p["title"]: p for p in self.pages if p["title"] != "Untitled"}
        self.cursor = "cur-" + uuid.UUID(int=rng.getrandbits(128)).hex[:12]
        self.fail_first_archive = fail_first_archive
        self.archived = set()

    def call(self, name, args):
        f = getattr(self, f"_{name}_{args.get('action')}", None)
        return f(args) if f else {"error": "unsupported in bench"}

    def _find(self, pid):
        k = norm_id(pid)
        return next((o for o in self.pages + self.rows if norm_id(o["id"]) == k), None) if k else None

    def _workspace_search(self, a):
        q = str(a.get("query") or "").casefold()
        kind = (a.get("filter") or {}).get("object") if isinstance(a.get("filter"), dict) else None
        lim = max(1, min(100, int(a["limit"]))) if isinstance(a.get("limit"), (int, float)) else 100  # spec: number
        hits = sorted((o for o in self.pages + [self.db] if o["id"] not in self.archived
                       and q in o["title"].casefold() and (not kind or o["object"] == kind)),
                      key=lambda o: o["last_edited_time"], reverse=True)
        more = len(hits) > lim
        return {"action": "search", "total": min(lim, len(hits)), "results": [dict(o) for o in hits[:lim]],
                "has_more": more, "next_cursor": "cur-search-2" if more else None}

    def _pages_get(self, a):
        p = self._find(a.get("page_id"))
        if not p or p["id"] in self.archived:
            return {"error": "not found", "page_id": a.get("page_id")}
        out = {"action": "get", "page_id": p["id"], "url": p["url"], "last_edited_time": p["last_edited_time"],
               "archived": False, "properties": {"title": {"type": "title", "title": p["title"]}}}
        if p["title"] != LONG_TITLE:
            out["content"] = f"# {p['title']}\n- 메모 1\n- 메모 2" if p["title"] != "Untitled" else ""
        elif a.get("content_cursor") == self.cursor:
            out.update(content=LONG[1], has_more=False)
        else:
            out.update(content=LONG[0], has_more=True, content_truncated=True, next_cursor=self.cursor)
        return out

    def _pages_archive(self, a):
        ids = [a["page_id"]] if a.get("page_id") else list(a.get("page_ids") or [])
        if not ids:
            return {"error": "page_id required"}
        done = []
        for pid in ids:
            p = self._find(pid)
            if self.fail_first_archive or not p or p["id"] in self.archived:
                self.fail_first_archive = False
                return {"error": "not found", "page_id": pid}
            self.archived.add(p["id"])
            done.append({"page_id": p["id"], "archived": True})
        return {"action": "archive", "processed": len(done), "results": done}

    def _pages_update(self, a):  # update(archived=true) is the other archive path in the real spec
        return self._pages_archive(a) if a.get("archived") is True else {"error": "unsupported in bench"}

    def _databases_query(self, a):
        if norm_id(a.get("database_id")) != norm_id(self.db["id"]):
            return {"error": "not found", "database_id": a.get("database_id")}
        f = dumps(a.get("filters") or {})
        rows = [r for r in self.rows if "Done" not in f or r["status"] == "Done"]
        return {"action": "query", "total": len(rows), "results": [
            {"id": r["id"], "url": r["url"], "properties": {"Name": r["title"], "Status": r["status"]}}
            for r in rows]}


READ_RESULT = {"type": "function", "function": {"name": "read_result", "description":
    "Open a stored tool result by its handle (r:N). Give one of: range=[start,end] (list items, end exclusive), "
    "query (substring over titles/text), path (e.g. \"results[2].id\"). Returns the matching part in full.",
    "parameters": {"type": "object", "properties": {"handle": {"type": "string"},
        "range": {"type": "array", "items": {"type": "integer"}}, "query": {"type": "string"},
        "path": {"type": "string"}}, "required": ["handle"]}}}


class Store(dict):
    """Store arm: handle r:N -> full result. read() is the read_result tool."""
    def put(self, r):
        h = f"r:{len(self) + 1}"
        self[h] = r
        return h

    def read(self, a):
        h = a.get("handle")
        if h not in self:  # only handles shown to this conversation exist here
            return {"error": "unknown handle", "handle": h}
        r, lst = self[h], items_of(self[h])
        if a.get("path"):
            v = r
            try:
                for t in re.findall(r"[^.\[\]]+", str(a["path"])):
                    v = v[int(t)] if isinstance(v, list) else v[t]
            except (KeyError, IndexError, ValueError, TypeError):
                return {"error": "path not found", "handle": h, "path": a["path"]}
            return {"handle": h, "path": a["path"], "value": v}
        if a.get("query") and lst is not None:
            q = str(a["query"]).casefold()
            hits = [x for x in lst if q in dumps(x).casefold()]
            return {"handle": h, "query": a["query"], "matches": len(hits), "results": hits}
        rg = a.get("range")
        if isinstance(rg, list) and len(rg) == 2 and all(isinstance(x, int) for x in rg) and lst is not None:
            s, e = rg
            return {"handle": h, "range": [s, e], "count": len(lst), "results": lst[s:e]}
        return r


def title_of(x):
    t = x.get("title") if isinstance(x, dict) else None
    return t if isinstance(t, str) or not isinstance(x, dict) else ((x.get("properties") or {}).get("title") or {}).get("title")


def summary(r):
    lst = items_of(r)
    if lst is not None:
        top = {k: v for k, v in r.items() if isinstance(v, (str, int, float, bool, type(None)))
               and not re.fullmatch(r"[0-9a-f]{32}", norm_id(v))} if isinstance(r, dict) else {}  # no IDs
        return {"type": "list", "count": len(lst), **top, "keys": sorted({k for x in lst if isinstance(x, dict) for k in x}),
                "preview": [title_of(x) for x in lst[:3]]}
    return {"type": "object", "keys": list(r), "title": title_of(r)} if isinstance(r, dict) else str(r)[:200]
