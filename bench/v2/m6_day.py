#!/usr/bin/env python3
"""m6_day.py -- v2 M6 (docs/plans/v2-partition-1.md, "Measurements"): a day costs <= $1 and caches >= 70%.

Live:    <venv>/bin/python bench/v2/m6_day.py --root-model P:M --price-in X --price-out Y
             [--summarize-model P:M] [--worker-model P:M] [--night-model P:M] [--effort E]
Dry run: python3 bench/v2/m6_day.py --dry-run   (a rule-based fake router: no network, no key; checks the scoring
         on a hand-made case, then the whole day's output JSON and pass/fail)

The real Clone, assembler, router and fold run in a temp home (PATHFINDER_HOME), with fake_notion's world as files
behind `read_file`. The day is m6_day.json: user items as root turns, signal envelopes (`signals.render_envelope`)
as `signal` items, each child a fresh clone (spawn parent root, role worker, its brief as the first message), one
night clone (role night) given a digest of the day's ledger; a gating item's gate is approved `approve_after_s`
later and its report item runs then. Usage comes only from the ledger's `assistant`/`summary` events. Prices
(per million tokens) come from the command line; cache read 0.1x, write 2x (the product's 1-hour TTL; adjusted,
the 5-minute comparison, 1.25x). Cold adjustment: an item 300 s (ttl_1h: 3600 s) or more after the previous request, or
the day's first, has its first request's cache reads repriced as writes. Pass (ttl_1h): cost <= $1, hit rate >= 0.70.
An item with an `error` event fails the day (printed, kept in its row). Output: docs/measurements/v2-m6-<model>-<date>.json.
"""
import argparse, asyncio, datetime, json, math, sys, tempfile, uuid
from pathlib import Path

import fake_notion, m6_stub  # next to this file; m6_stub puts this tree's pathfinder on sys.path
from m6_stub import FakeRouter, Home, day_rule, requests
from pathfinder import clock, memory, signals
from pathfinder.clone import Item

SPEC = Path(__file__).with_name("m6_day.json")
COMPOSITION = {"user": 30, "signal": 20, "child": 5, "night": 1}
READ, WRITE_5M, WRITE_1H = 0.1, 1.25, 2.0
COLD_5M, COLD_1H = 300, 3600
MAX_COST, MIN_HIT = 1.00, 0.70


def check_spec(spec) -> None:
    got = {k: sum(i["kind"] == k for i in spec["items"]) for k in COMPOSITION}
    users = [i for i in spec["items"] if i["kind"] == "user"]
    assert got == COMPOSITION, got
    assert sum(bool(i.get("tools")) for i in users) >= 8 and sum("approve_after_s" in i for i in users) == 2
    assert [i["at"] for i in spec["items"]] == sorted(i["at"] for i in spec["items"]), "items out of time order"


def world(home: Path, spec) -> None:
    w, ws = fake_notion.World(), home / "workspace" / "notion"
    ws.mkdir(parents=True)
    (home / "USER.md").write_text(spec["user_md"], encoding="utf-8")  # a fake profile: memory.context's part
    (ws / "pages.json").write_text(fake_notion.dumps({"results": w.pages}), encoding="utf-8")
    (ws / "lecture-db.json").write_text(fake_notion.dumps({"results": w.rows}), encoding="utf-8")
    (ws / "lecture-notes.md").write_text("\n".join(fake_notion.LONG), encoding="utf-8")


def digest(events, limit) -> str:
    """The day's ledger as text for the night run: each event's text, or a result's body, cut at `limit`."""
    lines = [f"{e['type']} {e.get('clone', '')}: {e.get('text') or json.dumps(e.get('body'), ensure_ascii=False)}"
             for e in events if e.get("text") or e["type"] == "result"]
    return "\n".join(lines)[:limit]


async def day(home: Home, spec) -> list[dict]:
    rows, now, prev, session = [], {"ts": 0.0}, None, spec["session"]
    agenda = signals.AGENDA_HEADER + "\n" + "\n".join(spec["agenda"])  # as signals.context joins the senses' lines

    async def resident():  # the daemon's three parts at the item's scripted time
        return [clock.now_line(now["ts"]), memory.context(home.path, home.state), agenda]

    async def run(clone, item, at, label):
        nonlocal prev
        now["ts"], mark = at, home.ledger.last_seq
        end = await clone.run(item)
        ev = home.events(mark)
        use = requests(ev)  # a failed request's ledger text is kept in the row: the temp home is deleted after the run
        failed = [{"status": e.get("status"), "message": e.get("message", "")[:200]} for e in ev if e["type"] == "error"]
        rows.append({"at": datetime.datetime.fromtimestamp(at).strftime("%H:%M:%S"), "kind": label, "clone": clone.id,
                     "role": clone.role, "end": end, "gap_s": None if prev is None else round(at - prev),
                     "requests": len(use), "usage": use, "folds": sum(e["type"] == "summary" for e in ev),
                     "tools": [e["tool"] for e in ev if e["type"] == "result"],
                     "gates": [e["seq"] for e in ev if e["type"] == "gate"],
                     "errors": len(failed), **({"error": failed[0]} if failed else {})})
        prev = at if use else prev
        return rows[-1]

    root = home.clone("root", "root", resident)
    for it in spec["items"]:
        at = datetime.datetime.fromisoformat(f"{spec['date']} {it['at']}").timestamp()  # HH:MM or HH:MM:SS
        if it["kind"] == "user":
            row = await run(root, Item("user", it["text"], session), at, "user")
        elif it["kind"] == "signal":
            sig = signals.Signal(it["sense"], it["signal"], f"m6-{it['at']}", it["summary"], it["fields"], it["trust"])
            row = await run(root, Item("signal", signals.render_envelope([sig]), session, origin="signal"), at, "signal")
        else:
            child = it["kind"] == "child"
            cid = f"{'w' if child else 'night'}-{uuid.uuid4().hex[:8]}"
            home.record("spawn", clone=cid, parent="root", preset="worker" if child else "night")
            brief = it["brief"] if child else f"{it['brief']}\n\n{digest(home.events(), it['digest_chars'])}"
            row = await run(home.clone(cid, "worker" if child else "night", resident),
                            Item("user", brief, session, origin="system"), at, it["kind"])
            row["brief_chars"] = len(brief)
        row["needs_tools"] = bool(it.get("tools"))
        if "approve_after_s" in it:
            row["approved"] = [g for g in row["gates"] if root.decide(g, "approved", "user")]
            for _ in range(400 if row["approved"] else 0):  # the approved call runs on this loop: <= 20 s
                if home.queue:
                    break
                await asyncio.sleep(0.05)
        while home.queue:  # gate reports (and a say left at a turn's end), in lane order
            await run(root, home.queue.pop(0), at + it.get("approve_after_s", 0), "report")
    return rows


def score(rows, p_in, p_out, cold_s=None, write=WRITE_1H) -> dict:
    """Tokens by kind, cost in $ and hit rate; with `cold_s`, the first request of an item that came at least
    `cold_s` after the previous request (or first in the day) has its cache reads counted as writes."""
    t = dict.fromkeys(("input", "output", "cache_read", "cache_creation"), 0)
    for row in rows:
        cold = cold_s is not None and (row["gap_s"] is None or row["gap_s"] >= cold_s)
        for k, (i, o, read, made) in enumerate(row["usage"]):
            if k == 0 and cold:
                read, made = 0, made + read
            for name, n in zip(t, (i, o, read, made)):
                t[name] += n
    cost = ((t["input"] + t["cache_read"] * READ + t["cache_creation"] * write) * p_in + t["output"] * p_out) / 1e6
    prompt = t["input"] + t["cache_read"] + t["cache_creation"]
    return {"tokens": t, "cost_usd": cost, "hit_rate": round(t["cache_read"] / prompt, 4) if prompt else 0.0}


def report(rows, a, refs) -> dict:
    hour, errors = score(rows, a.price_in, a.price_out, COLD_1H), sum(bool(r["errors"]) for r in rows)
    return {"measurement": "v2-m6", "models": refs, "effort": a.effort or "api default", "date": a.date,
            "prices_per_mtok": {"in": a.price_in, "out": a.price_out, "cache_read": READ, "write_5m": WRITE_5M,
                                "write_1h": WRITE_1H},
            "ttl_1h_note": "the product's pricing (1-hour writes at 2x) and the pass figure; adjusted: the 5-minute one",
            "requests": sum(r["requests"] for r in rows), "folds": sum(r["folds"] for r in rows), "errors": errors,
            "raw": score(rows, a.price_in, a.price_out), "adjusted": score(rows, a.price_in, a.price_out, COLD_5M, WRITE_5M),
            "ttl_1h": hour, "pass": hour["cost_usd"] <= MAX_COST and hour["hit_rate"] >= MIN_HIT and not errors,
            "criteria": {"max_cost_usd": MAX_COST, "min_hit_rate": MIN_HIT, "cold_after_s": COLD_1H, "max_errors": 0},
            "items": rows}


def check_score() -> None:
    """The cold adjustment and the 1-hour figure on a hand-made day of three items."""
    rows = [{"gap_s": None, "usage": [[10, 5, 0, 1000]]}, {"gap_s": 60, "usage": [[10, 5, 1000, 100]]},
            {"gap_s": 600, "usage": [[10, 5, 1000, 100], [10, 5, 1100, 50]]}]
    raw, adj, hour = score(rows, 1, 5), score(rows, 1, 5, COLD_5M, WRITE_5M), score(rows, 1, 5, COLD_1H)
    assert raw["tokens"] == {"input": 40, "output": 20, "cache_read": 3100, "cache_creation": 1250}, raw
    assert adj["tokens"]["cache_read"] == 2100 and adj["tokens"]["cache_creation"] == 2250, adj
    assert hour == raw and math.isclose(raw["cost_usd"], (40 + 310 + 1250 * 2 + 100) / 1e6), (raw, hour)
    assert math.isclose(adj["cost_usd"], (40 + 210 + 2250 * 1.25 + 100) / 1e6) and adj["hit_rate"] == round(2100 / 4390, 4)


def main():
    ap = argparse.ArgumentParser(description="v2 M6: a day costs <= $1 and caches >= 70%")
    ap.add_argument("--dry-run", action="store_true", help="fake router, no network, no key; checks the output")
    ap.add_argument("--root-model", default="", help="provider:model (the provider is configured as api anthropic)")
    for role in ("summarize", "worker", "night"):
        ap.add_argument(f"--{role}-model", default="", help="provider:model; default: the root's")
    ap.add_argument("--effort", default="", choices=["", "low", "medium", "high", "xhigh", "max"],
                    help="providers.<name>.effort for every role ('' = the API default, the product's)")
    ap.add_argument("--price-in", type=float, help="$ per million input tokens")
    ap.add_argument("--price-out", type=float, help="$ per million output tokens")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--out", default=str(m6_stub.ROOT / "docs" / "measurements"))
    a = ap.parse_args()
    if a.dry_run:
        a.root_model, a.price_in, a.price_out = a.root_model or "fake:fake-model", a.price_in or 1.0, a.price_out or 5.0
    elif ":" not in a.root_model or a.price_in is None or a.price_out is None:
        ap.error("--root-model provider:model, --price-in and --price-out are required (no prices in code)")
    refs = {"root": a.root_model, **{r: getattr(a, f"{r}_model") or a.root_model for r in ("summarize", "worker", "night")}}
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    check_spec(spec)
    check_score()
    with tempfile.TemporaryDirectory(prefix="pf-m6-") as tmp:
        m6_stub.configure(Path(tmp), refs, a.effort)
        world(Path(tmp), spec)
        home = Home(Path(tmp), FakeRouter(refs, day_rule) if a.dry_run else None)
        rows = asyncio.run(day(home, spec))
        resident = next(e["resident"] for e in home.events() if e["type"] == "turn_start")  # the first item's
        home.state.close()
    out = report(rows, a, refs) | {"resident_chars": len(resident)}
    model = refs["root"].partition(":")[2].replace("/", "-").replace(":", "-")
    path = Path(tempfile.gettempdir() if a.dry_run else a.out) / f"v2-m6-{model}-{a.date}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name in ("raw", "adjusted", "ttl_1h"):
        print(f"{name:<9} ${out[name]['cost_usd']:.4f}  hit {out[name]['hit_rate']:.3f}  {out[name]['tokens']}")
    print(f"requests {out['requests']}  folds {out['folds']}  resident {len(resident)} chars  pass {out['pass']}  -> {path}")
    print(*(f"error row {i}: {r['kind']} at {r['at']} ({r['clone']}) {r['error']['status']}: {r['error']['message']}"
            for i, r in enumerate(rows) if r["errors"]), f"errors: {out['errors']} (must be 0)", sep="\n")
    if a.dry_run:
        assert {k: sum(r["kind"] == k for r in rows) for k in [*COMPOSITION, "report"]} == {**COMPOSITION, "report": 2}
        assert not report([{**rows[0], "errors": 1}], a, refs)["pass"], "an item with an error must fail the day"
        assert next(r for r in rows if r["kind"] == "night")["brief_chars"] == len(spec["items"][-1]["brief"]) + 30002
        assert spec["user_md"].splitlines()[1] in resident and signals.AGENDA_HEADER in resident
        assert sum(bool(r["tools"]) for r in rows if r["kind"] == "user" and r["needs_tools"]) >= 8
        assert all(r["requests"] and not r["errors"] for r in rows) and out["folds"] >= 1
        assert sum(len(r.get("approved", [])) for r in rows) == 2 and out["raw"]["hit_rate"] > out["adjusted"]["hit_rate"]
        assert out["pass"] == (out["ttl_1h"]["cost_usd"] <= MAX_COST and out["ttl_1h"]["hit_rate"] >= MIN_HIT)
        print("dry run: PASS (scoring, composition, gates, folds, output shape)")
    return 0 if out["pass"] or a.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
