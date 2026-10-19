"""The result store, model side (V3). A tool result is the ledger's `result` event and its handle `r:N` is
that event's seq. The model sees a rule-rendered summary with the handle (`render`), opens more with
`read_result` (`read`) over the handles its window has shown, and the writer cuts a body to the store cap
(`cap`) before it is appended. Third-party text goes through `tools.clean` on the way to the model only."""
import json
import re

from . import tools

STORE_CAP = 256 * 1024  # bytes of a body's compact JSON (or of its text) kept in the ledger
READ_CAP = 6000         # chars of one read_result output (Deviation 6)
RENDER_CAP = 2000       # chars of one rendered result: the handle and summary, or the error inline
UNKNOWN = {"error": "unknown handle"}  # unknown and unseen answer alike, so a guessed handle learns nothing
INLINE = {"read_result", "search_tools"}  # tools whose output the model sees in full (each ≤ 6,000), not summarised
_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,}", re.I)  # uuid, or a hex run
_SCALAR = (str, int, float, bool, type(None))
_RESERVED = ("type", "count", "keys", "preview", "chars", "lines", "stored")

READ_RESULT = {
    "name": "read_result",
    "description": (
        "Open a stored tool result by its handle (r:N, as shown with the result). Without a selector it "
        "returns the whole result. path (e.g. \"results[2].id\" or \"content\") narrows to one part first; "
        "then range=[start, end] (items for lists, characters for text; end exclusive) or query "
        "(case-insensitive substring over a list's text fields, or a text's lines; `at` gives the item indexes "
        "or the lines' character offsets) selects from it. "
        "Output is capped at 6,000 characters; a cut list ends with a note \"shown K of N\"."),
    "parameters": {"type": "object", "properties": {
        "handle": {"type": "string"},
        "range": {"type": "array", "items": {"type": "integer"}},
        "query": {"type": "string"},
        "path": {"type": "string"}}, "required": ["handle"]},
}


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _value(body):
    """A body as the store sees it: a string that parses as a JSON list or object is that JSON."""
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except ValueError:
            return body
        return parsed if isinstance(parsed, (dict, list)) else body
    return body if isinstance(body, (dict, list)) else _dumps(body)


def _items(value):
    """The list inside a result: a bare list, or an object's `results` list; else None."""
    # ponytail: only `results` is recognised (Notion, MCP searches); add other list keys when a tool needs one
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        return value["results"]
    return None


def _with_items(value, items):
    return items if isinstance(value, list) else {**value, "results": items}


def _line(text: str, n: int) -> str:
    """Someone else's text on one line, at most `n` chars, every ID-shaped run replaced (after the cut, so a
    long run never pulls far text into the line)."""
    return _ID.sub("…", tools.oneline(text, n))


def _has_id(value) -> bool:
    return bool(_ID.search(tools.clean(str(value))))


def _title(item):
    """An item's title: `title`, else `name`, else Notion's title property (the one whose `type` is "title",
    whatever its key; else the key `title`); a rich-text list joins its parts' `plain_text` (or `text.content`).
    None without one."""
    if not isinstance(item, dict):
        return None
    title = item.get("title", item.get("name"))
    if not isinstance(title, (str, list)):
        props = item.get("properties")
        prop = next((p for p in props.values() if isinstance(p, dict) and p.get("type") == "title"),
                    props.get("title")) if isinstance(props, dict) else None
        title = prop.get("title") if isinstance(prop, dict) else prop
    if isinstance(title, list):  # Notion rich text
        parts = [p.get("plain_text", p["text"].get("content") if isinstance(p.get("text"), dict) else None)
                 for p in title if isinstance(p, dict)]
        title = "".join(s for s in parts if isinstance(s, str)) or None
    return _line(title, 80) if isinstance(title, str) else None


def _id_key(text: str) -> str:
    return text.strip().replace("-", "").lower()  # a UUID with or without hyphens, any case


def title(state, seen: set[int], args: dict) -> str:
    """A gate's title (Q1): every ID-shaped argument value, and every ID-shaped element of a list value, that is
    the `id` of an item (or of the object) in a seen result's body becomes `<arg>: <title>` (`_title`'s rule);
    arguments not named `parent*` first, at most 3, joined by "; "; "" when nothing matches."""
    ranked = sorted(args.items(), key=lambda kv: kv[0].startswith("parent"))
    wanted = [(arg, v) for arg, value in ranked for v in (value if isinstance(value, list) else [value])
              if isinstance(v, str) and _ID.fullmatch(v.strip())]
    names = {}
    for row in filter(None, map(state.result, sorted(seen) if wanted else ())):  # newer results win
        value = _value(row["body"])
        for item in _items(value) or [value]:
            name = _title(item)
            if name is not None and isinstance(item.get("id"), str):
                names[_id_key(item["id"])] = name
    found = [f"{tools.oneline(arg, 40)}: {names[_id_key(v)]}" for arg, v in wanted if _id_key(v) in names]
    return "; ".join(found[:3])


def _scalars(obj: dict) -> dict:
    """An object's top-level scalar fields without IDs, strings on one line: a field whose value looks like
    an ID, or whose key names one (`id`, `*_id`, `*Id`: Slack, Drive), is left out, and so is a key that
    cleans to one of the summary's own."""
    out = {}
    for k, v in obj.items():
        key = _line(k, 40)
        if (isinstance(v, _SCALAR) and key not in _RESERVED and not _has_id(k) and not _has_id(v)
                and key != "id" and not key.endswith(("_id", "Id"))):
            out[key] = _line(v, 120) if isinstance(v, str) else v
    return out


def _keys(objs) -> list:
    """The keys the objects use, sorted, without ID-shaped keys; at most 20."""
    return sorted({_line(k, 40) for o in objs for k in o if not _has_id(k)})[:20]


def summary(body) -> dict:
    """The rule renderer: a list → type, count, the top-level scalar fields, keys, the first 3 titles;
    an object → type, keys, scalar fields; text → type, chars, lines, the first line. Never an ID."""
    value = _value(body)
    items = _items(value)
    if items is not None:
        top = _scalars(value) if isinstance(value, dict) else {}
        return {"type": "list", "count": len(items), **top,
                "keys": _keys(x for x in items if isinstance(x, dict)),
                "preview": [t for t in map(_title, items[:3]) if t is not None]}
    if isinstance(value, dict):
        return {"type": "object", "keys": _keys([value]), **_scalars(value)}
    lines = value.splitlines()
    return {"type": "text", "chars": len(value), "lines": len(lines),
            "first_line": _line(lines[0], 120) if lines else ""}


def _clipped(make, text: str, limit: int) -> dict:
    """make(text), its compact JSON within `limit` chars: text cut from its end as far as that needs
    (each raw char cut shortens the JSON by at least one)."""
    out = make(text)
    over = len(_dumps(out)) - limit
    return out if over <= 0 else make(text[:max(0, len(text) - over)])


def render(event: dict) -> str:
    """The tool message for a `result` event (its body included): `{"handle": "r:N", "summary": S}`, a
    failed call's error inline instead of a summary; a body cut at the store cap says how much is stored."""
    handle = f"r:{event['seq']}"
    body = event.get("body")
    if not event.get("ok"):
        text = tools.clean(body if isinstance(body, str) else _dumps(body))
        return _dumps(_clipped(lambda t: {"handle": handle, "error": t}, text, RENDER_CAP))
    found = summary(body)
    cut = event.get("cut")
    if cut:
        found["stored"] = (f"stored {cut['kept']} of {cut['of']} (store cap)" if "kept" in cut
                           else f"stored {found.get('chars')} of {cut['chars']} chars (store cap)")
    out = _dumps({"handle": handle, "summary": found})
    if len(out) > RENDER_CAP:  # wide objects: the counts survive, the field lists go
        out = _dumps({"handle": handle, "summary": {k: found[k] for k in ("type", "count", "chars", "lines", "stored")
                                                    if k in found}})
    return out


def cap(body) -> tuple[object, dict | None]:
    """The store cap, applied by the writer before `ledger.append`: a body over STORE_CAP bytes (a string
    as its text, anything else as its compact JSON) is cut, a list at an item boundary (`{"kept": k, "of":
    n}`; a string holding JSON stays a string), anything else as text at a char boundary (`{"chars": n}`,
    n = its length before the cut). The dict is the event's `cut`; None when nothing was cut."""
    text = body if isinstance(body, str) else _dumps(body)
    if len(text.encode("utf-8", "surrogatepass")) <= STORE_CAP:
        return body, None
    value = _value(body)
    items = _items(value)
    if items is not None:
        k = _fit(len(items), lambda k: len(_dumps(_with_items(value, items[:k])).encode("utf-8", "surrogatepass")) <= STORE_CAP)
        if k > 0:  # not even one item fits: cut as text
            kept = _with_items(value, items[:k])
            return (_dumps(kept) if isinstance(body, str) else kept), {"kept": k, "of": len(items)}
    return text.encode("utf-8", "surrogatepass")[:STORE_CAP].decode("utf-8", "ignore"), {"chars": len(text)}


def _fit(n: int, ok) -> int:
    """The largest k in 0..n with ok(k), where ok(k) implies ok(j) for every j < k; -1 if none."""
    lo, hi = -1, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid - 1)
    return lo


def _shown(out: dict) -> dict:
    """A read_result output within READ_CAP chars, strings cleaned: its `results` cut at an item boundary
    with a `{"note": "shown K of N"}` item last, else the whole output cut as text."""
    out = tools.map_strings(out, tools.clean)
    if len(_dumps(out)) <= READ_CAP:
        return out
    items = out.get("results")
    if isinstance(items, list):
        def wrap(k):
            return {**out, "results": items[:k] + [{"note": f"shown {k} of {len(items)}"}]}
        k = _fit(len(items), lambda k: len(_dumps(wrap(k))) <= READ_CAP)
        if k > 0:  # not even one item fits: cut as text
            return wrap(k)
    text = out["value"] if isinstance(out.get("value"), str) else _dumps(out)
    return _clipped(lambda t: {"handle": out["handle"], "text": t, "note": f"shown {len(t)} of {len(text)} chars"},
                    text, READ_CAP)


def _texts(value):
    """Every string value inside `value` (not the keys)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _texts(v)


def read(state, seen: set[int], args: dict) -> tuple[bool, object]:
    """`read_result`. A handle that is not an `r:N` in `seen` (the window's handles) or not a stored result
    is refused with UNKNOWN before anything is read. `path` narrows first; then `range` selects list items
    or text characters, `query` list items or text lines (`at`: indexes, or char offsets); without them the whole part (a list as `results`, anything else as `value`). Returns (ok, output within READ_CAP)."""
    match = re.fullmatch(r"r:(\d+)", str(args.get("handle", "")).strip())
    row = state.result(int(match[1])) if match and int(match[1]) in seen else None
    if row is None:
        return False, dict(UNKNOWN)
    out, value = {"handle": f"r:{row['seq']}"}, _value(row["body"])
    if args.get("path"):
        out["path"] = str(args["path"])
        try:
            for key in re.findall(r"[^.\[\]]+", out["path"]):
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, ValueError, TypeError):
            return False, {**out, "error": "path not found"}
    query, span = args.get("query"), args.get("range")
    selected = query or span is not None
    if span is not None and not (isinstance(span, list) and len(span) == 2 and all(type(x) is int for x in span)):
        return False, {**out, "error": "range must be [start, end]"}
    if isinstance(value, str) and selected:  # text: range over characters, query over lines
        if query:
            needle, at, hits, pos = str(query).casefold(), [], [], 0
            for line in value.splitlines(keepends=True):
                if needle in line.casefold():
                    at.append(pos)  # a char offset, so a range can follow
                    hits.append(line.rstrip("\r\n"))
                pos += len(line)
            out.update(query=str(query), matches=len(at), at=at, results=hits)
        else:
            out.update(range=span, chars=len(value), value=value[span[0]:span[1]])
        return True, _shown(out)
    items = None if isinstance(value, str) else _items(value)
    if isinstance(value, dict) and items is not None:  # the list's neighbours (cursor, total) come along
        out = {**{k: v for k, v in value.items() if k != "results"}, **out}
    if items is None:
        if selected:
            return False, {**out, "error": "range and query need a list or text; pick one with path"}
        return True, _shown({**out, "value": value})
    if query:
        needle = str(query).casefold()
        at = [i for i, x in enumerate(items) if any(needle in t.casefold() for t in _texts(x))]
        out.update(query=str(query), matches=len(at), at=at, results=[items[i] for i in at])
    elif span is not None:
        out.update(range=span, count=len(items), results=items[span[0]:span[1]])
    else:
        out.update(count=len(items), results=items)
    return True, _shown(out)
