"""The clone runtime (V11, V22): one lane item at a time as a turn of model steps, each request built by the
assembler from the ledger-derived state and sent through the router, each tool call through the broker (in a
thread) and recorded as a capped `result`. A gated call is handed off (V22): the model gets the waiting status
and the turn goes on; `decide` later runs the call once and queues a report item. Steps and tokens are counted
from the turn's recorded `assistant` events, so nothing the budget needs lives only in memory. After a turn the
window may fold (V5). `restore` closes what a dead daemon left open (V12)."""
import asyncio
import collections
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from . import assemble, config, results, tools
from .broker import Broker, Gate, unwrap
from .llm import LLMError

TOKEN_BUDGET = 100_000  # per item: input + cache_creation + output tokens (cache reads excluded)
FAILS_TO_STOP = 3       # the same tool failing this many times in a row stops the turn's tool use
BUDGET_NOTE = ("[harness] This turn's step or token budget is spent. Do not call tools now: tell the user where "
               "things stand and what is left to do.")
FAILED_NOTE = ("[harness] {tool} failed 3 times in a row. Do not call tools now: tell the user what failed and "
               "what you would try next.")
NOT_RUN = {"budget": "not run: budget spent", "error": "not run: stopped after repeated failures",
           "max_tokens": "not run: the answer was cut at the output limit"}
SKIPPED = "not run: skipped after a refusal or an error in this batch"
USER_UNKNOWN = "outcome unknown: interrupted by the user while it ran"
NOT_RUN_INTERRUPTED = "not run: interrupted"
REFUSED_LINE = "the model declined; this exchange is left out of the conversation"
USAGE_SPENT = ("input_tokens", "cache_creation_input_tokens", "output_tokens")
UNKNOWN = "?unknown"  # every call of a name the registry lacks counts as this one tool: the third stops tool use (Q7)
RECIPIENT = ("to", "recipient", "recipients", "cc", "email", "channel")  # a send's gate names the first present
FLOOR = ("destructive", "external_send", "unknown")  # gates no "always" scope may cover (§8)
SCOPES = ("", "tool", "server", "session")


@dataclass(frozen=True)
class Item:
    """One lane item. `kind`: user | schedule | continue | signal; `gate` (a report item): the gate it reports.
    `turn`: the turn its opening `user` event was already recorded under (the daemon's say); "" = the clone
    records `text` as the opening `user` event of a new turn."""
    kind: str
    text: str
    session: str
    origin: str = "user"
    gate: int = 0
    turn: str = ""


def _spent(rows) -> tuple[int, int, dict]:
    """(steps, budget tokens, summed usage) of a turn's rows."""
    usage = {}
    for row in rows:
        for key, n in (row.get("usage") or {}).items() if row["type"] == "assistant" else ():
            usage[key] = usage.get(key, 0) + n
    steps = sum(row["type"] == "assistant" for row in rows)
    return steps, sum(usage.get(k, 0) for k in USAGE_SPENT), usage


def _failed(e: LLMError) -> list[dict]:
    """Every failed attempt of a request that raised: the router's list, plus the last error when it did not
    fall back (a 400, a config error), which the router raises without listing."""
    fell = e.fallbacks and e.fallbacks[-1]["status"] == e.status and e.fallbacks[-1]["message"] == e.message
    return e.fallbacks if fell else [*e.fallbacks, {"model": "", "status": e.status, "message": e.message}]


def _failing(rows) -> str:
    """The tool whose last FAILS_TO_STOP results in this turn all failed, or "" - UNKNOWN for names the
    registry lacks (read_result and call_tool are the clone's own). A skipped call never ran: not counted."""
    oks = collections.defaultdict(list)
    for row in rows:
        if row["type"] == "result" and not row.get("skipped"):
            known = row["tool"] in tools.TOOLS or row["tool"] in (results.READ_RESULT["name"], tools.CALL_TOOL["name"])
            oks[row["tool"] if known else UNKNOWN].append(row["ok"])
    return next((t for t, seen in oks.items() if len(seen) >= FAILS_TO_STOP and not any(seen[-FAILS_TO_STOP:])), "")


class Clone:
    def __init__(self, id, role, *, home, state, record, router, send, enqueue, resident):
        """`record(type, **fields) -> event` is the daemon's ledger-then-state writer (loop thread only);
        `send(session, msg)` reaches that session's screens; `enqueue(item)` puts an item on the lane;
        `resident()` gathers the resident block's parts once per item."""
        self.id, self.role, self.home = id, role, home
        self.db, self.record, self.router = state, record, router
        self.send, self.enqueue, self.resident = send, enqueue, resident
        self._start = 0           # the ledger's last seq at this daemon's start (restore): thinking up to it goes
        self._task = None         # the task running a turn, while one runs
        self._flag = False        # set by interrupt(): a cancel the clone asked for itself, not a shutdown
        self._events = set()      # interrupt Events of the running turn's tools and of approved calls
        self._steers = []         # (session, text) of says that arrived during a turn, recorded at a step boundary
        self._calls = set()       # tasks of approved calls (asyncio keeps only weak references)
        self._lists = {}          # session -> (hash of the tool list last sent, its thinking epoch) (Q8)

    @property
    def state(self) -> str:
        """running | parked, as the ledger says (the clones row)."""
        row = self.db.clone_row(self.id)
        return row["state"] if row else "parked"

    def steer(self, session: str, text: str) -> None:
        """A say while a turn runs: recorded as `user {steer: true}` right before the next request of an item of
        `session` is assembled, so seq order is the order the model saw; one of the running item's session left
        at its turn's end becomes a `continue` item. The daemon steers only a say of the running item's session."""
        self._steers.append((session, text))

    def _take(self, session) -> list[str]:
        mine = [text for s, text in self._steers if s == session]
        self._steers = [(s, text) for s, text in self._steers if s != session]
        return mine

    def interrupt(self) -> None:
        """Stop what runs now: the turn's step and every approved call still running. Open gates stay open.
        A shutdown already cancelling the turn wins: then nothing is marked as the clone's own interrupt."""
        if self._task is not None and self._task.cancelling():
            return
        self._flag = True
        for event in self._events:
            event.set()  # exec kills the process group; openai_compat stops streaming
        if self._task is not None:
            self._task.cancel()  # the Anthropic adapter returns what arrived; a tool's late return is dropped

    def _view(self, session) -> assemble.View:
        fold = self.db.last_fold(self.id, session)
        return assemble.View(self.id, self.role, session,
                             max(self._start, fold["seq"] if fold else 0, self._lists.get(session, ("", 0))[1]))

    def _listed(self, session, specs) -> None:
        """Q8 (a): a tool list unlike the one last sent in `session` moves its thinking epoch to the ledger's
        last seq - a thinking signature is bound to the list it was made with (one cache rewrite)."""
        digest = hashlib.sha256(json.dumps(specs).encode()).hexdigest()
        sent, epoch = self._lists.get(session, (digest, 0))
        self._lists[session] = (digest, epoch if sent == digest else self.db.cursor)

    def _rows(self, session, turn, after) -> list[dict]:
        return [r for r in self.db.context(self.id, session, after) if r["turn"] == turn]

    def _errors(self, ev, step, failed) -> None:
        for f in failed:
            self.record("error", **ev, step=step, model=f["model"], status=f["status"], message=f["message"][:300])

    def _result(self, ev, step, call_id, tool, ok, body, **extra) -> dict:
        tool = tool[:64]  # model text: bounded (`assistant.blocks` keeps the raw name)
        body, cut = results.cap(body)
        event = self.record("result", **ev, trust="third_party", step=step, call_id=call_id, tool=tool, ok=ok,
                            body=body, **({"cut": cut} if cut else {}), **extra)
        self.send(ev["session"], {"type": "tool", "name": tool, "ok": ok})
        return event

    # -- the turn

    async def run(self, item: Item) -> str:
        """One item end to end; returns its `turn_end` kind, or "" for a report item already merged into an
        earlier report turn. A shutdown (the task cancelled, not by interrupt()) records nothing more."""
        gates = self._reports(item) if item.gate else []
        if item.gate and not gates:
            return ""
        turn = item.turn or uuid.uuid4().hex[:12]
        ev = {"session": item.session, "turn": turn, "origin": item.origin, "clone": self.id}
        event = threading.Event()
        self._task, self._flag = asyncio.current_task(), False
        self._events.add(event)
        try:
            if gates:
                self.record("user", **{**ev, "origin": "system"}, gate=[g["seq"] for g in gates],
                            text="\n".join(self._report_line(g) for g in gates))
            elif not item.turn:
                self.record("user", **ev, trust="user" if item.origin == "user" else "third_party", text=item.text)
            try:
                parts = await self.resident()
            except asyncio.CancelledError:  # interrupted while gathering: the turn opens and ends at once
                if not self._flag:
                    raise
                parts = []
            start = self.record("turn_start", **ev, item=item.kind, resident=assemble.resident(parts))["seq"]
            self.send(item.session, {"type": "turn.start", "turn": turn})
            kind = await self._turn(item, ev, start, event, {g["result"] for g in gates} - {None})
        finally:
            self._task = None
            self._events.discard(event)
        steps, _, usage = _spent(self._rows(item.session, turn, start))
        self.record("turn_end", **ev, kind=kind, steps=steps, usage=usage)
        self.send(item.session, {"type": "turn.end", "turn": turn, "kind": kind})
        if left := self._take(item.session):  # said during the last step: a turn of their own, the user's
            self.enqueue(Item("continue", "\n".join(left), item.session))
        await self._fold(item.session)
        return kind

    async def _turn(self, item, ev, start, event, reported) -> str:
        """`reported`: the results a report turn's opening message names, seen by the model even when the
        turn they were recorded under is out of the window (a refused turn)."""
        cfg = config.load(self.home)
        # D76, Q4 (b): one list for every origin, the natives and call_tool; an MCP tool is found with search_tools
        # and called through call_tool, so an install never changes the list. A copy: an install thread edits
        # TOOLS. Names resolve in the live registry: a tool search_tools lists mid-turn is callable.
        specs = [s.schema()["function"] for s in dict(tools.TOOLS).values() if not s.name.startswith("mcp__")]
        specs.append(tools.CALL_TOOL)
        self._listed(item.session, specs)
        broker = Broker(tools.TOOLS, item.origin, cfg, session_grants=lambda: self.db.session_grants(item.session),
                        control=SimpleNamespace(interrupt=event, session=item.session))  # edit_memory's evidence
        end = ""  # set once the turn must end: the next step is its last, and its calls do not run
        while True:
            steps, tokens, _ = _spent(self._rows(item.session, ev["turn"], start))
            if self._flag:  # interrupted before the first step, while the resident block was gathered
                return self._interrupted(ev, steps)
            if not end and (steps >= cfg["max_steps"] or tokens >= TOKEN_BUDGET):
                end = "budget"
                self.record("user", **{**ev, "origin": "system"}, text=BUDGET_NOTE)
            for said in self._take(item.session):
                self.record("user", **ev, trust="user", steer=True, text=said)
            step, streamed = steps + 1, []

            def on_text(text):
                streamed.append(text)
                self.send(item.session, {"type": "delta", "text": text})

            req = assemble.assemble(self.db, self._view(item.session), specs)
            try:
                resp = await self.router.complete(req, on_text=on_text, cancel=event)
            except asyncio.CancelledError:  # openai_compat (a thread) or a resolve: nothing came back
                if not self._flag:
                    raise
                resp = None
            except LLMError as e:
                self._errors(ev, step, _failed(e))
                if streamed:  # §11 invariant 2: what reached the screen is in the ledger, whatever the ending
                    said = "".join(streamed)
                    blocks = [{"type": "text", "text": said}] if said.strip() else []  # a blank text block: 400
                    self.record("assistant", **ev, step=step, model="", blocks=blocks, stop_reason="error", usage={},
                                text=said, final=True)
                self.send(item.session, {"type": "error", "message": e.message[:300]})
                return "error"
            if resp is None or resp.stop_reason == "interrupted":
                if not self._flag and asyncio.current_task().cancelling():
                    raise asyncio.CancelledError  # a shutdown: the adapter returned, the daemon is going
                blocks = [b for b in resp.blocks if b["type"] == "text" and b.get("text", "").strip()] if resp else (
                    [{"type": "text", "text": "".join(streamed)}] if "".join(streamed).strip() else [])
                if blocks:  # only complete text blocks: no unsigned thinking, no half tool_use
                    self.record("assistant", **ev, step=step, model=resp.model if resp else "", blocks=blocks,
                                stop_reason="interrupted", usage=resp.usage if resp else {}, text="".join(streamed),
                                final=True)
                return self._interrupted(ev, step)
            self._errors(ev, step, resp.fallbacks)
            calls = [b for b in resp.blocks if b["type"] == "tool_use"]
            said = "".join(b["text"] for b in resp.blocks if b["type"] == "text")
            self.record("assistant", **ev, step=step, model=resp.model, blocks=resp.blocks,
                        stop_reason=resp.stop_reason, usage=resp.usage,
                        # a fallback after a partial stream: the screen got both attempts, so `text` holds both
                        text="".join(streamed) if resp.fallbacks and streamed else said,
                        final=bool(end) or resp.stop_reason != "tool_use")
            if resp.stop_reason == "refusal":
                self.send(item.session, {"type": "error", "message": REFUSED_LINE})
                return "refused"
            if resp.stop_reason == "max_tokens" or (end and calls):
                why = "max_tokens" if resp.stop_reason == "max_tokens" else end
                for call in calls:
                    self._result(ev, step, call["id"], unwrap(call["name"], call["input"])[0], False, NOT_RUN[why])
                if why == "max_tokens":
                    self.send(item.session, {"type": "error", "message": NOT_RUN[why]})
                return why
            if not calls:
                return end or "answer"
            if await self._batch(item, ev, step, calls, broker, reported) == "interrupted":
                return self._interrupted(ev, step)
            if tool := _failing(self._rows(item.session, ev["turn"], start)):
                end = "error"
                tool = "calling a tool name that does not exist" if tool == UNKNOWN else tool
                self.record("user", **{**ev, "origin": "system"}, text=FAILED_NOTE.format(tool=tool))

    def _interrupted(self, ev, step) -> str:
        self.record("interrupted", **ev, step=step, why="user")
        if asyncio.current_task().cancelling():
            asyncio.current_task().uncancel()  # our own cancel, handled: the lane goes on
        return "interrupted"

    async def _batch(self, item, ev, step, calls, broker, reported) -> str:
        """The step's calls in order (Q7 (a)), a `call_tool` unwrapped first (its `result` and gate name the real
        tool). The first failed call - a refusal, a denial, an error - skips the rest; a gate hands its call off
        and the batch goes on. Returns "interrupted" when interrupt() stopped a running call (it and the later
        ones are recorded)."""
        skip = False
        for i, call in enumerate(calls):
            call_id = call["id"]
            name, args, refused = unwrap(call["name"], call["input"])
            if skip:
                self._result(ev, step, call_id, name, False, SKIPPED, skipped=True)
                continue
            if refused:
                ok, body = False, refused
            elif name == results.READ_RESULT["name"]:
                seen = assemble.window_handles(self.db, self._view(item.session)) | reported
                ok, body = results.read(self.db, seen, args)
            else:
                try:
                    out = await asyncio.to_thread(broker.call, call_id, name, args, name != call["name"])
                except Gate as g:
                    self._gate(item, ev, step, g, reported)
                    continue
                except asyncio.CancelledError:
                    if not self._flag:
                        raise
                    self._result(ev, step, call_id, name, False, USER_UNKNOWN)
                    for later in calls[i + 1:]:
                        self._result(ev, step, later["id"], unwrap(later["name"], later["input"])[0], False,
                                     NOT_RUN_INTERRUPTED)
                    return "interrupted"
                ok = out["ok"]
                body = out["result"] if ok else out["error"]
            self._result(ev, step, call_id, name, ok, body)
            skip = not ok
        return ""

    def _gate(self, item, ev, step, g, reported) -> None:
        """Records a handed-off call's `gate` (Q1): its effect, the missed lookup, the title its ID arguments
        have in the seen results, a send's recipient, the item's text on one line; and shows it."""
        seen = assemble.window_handles(self.db, self._view(item.session)) | reported
        title = results.title(self.db, seen, g.arguments)
        extra = {"unknown": g.unknown} if g.unknown else {}
        to = next((g.arguments[k] for k in RECIPIENT if k in g.arguments), None)
        if g.effect == "external_send" and to is not None:
            extra["recipient"] = tools.oneline(", ".join(map(str, to)) if isinstance(to, list) else str(to), 120)
        gate = self.record("gate", **ev, step=step, call_id=g.id, tool=g.name, args=g.arguments, effect=g.effect,
                           title=title, brief=tools.oneline(item.text, 120), **extra)
        self.send(item.session, {"type": "gate", "id": gate["seq"], "tool": g.name, "args": gate["args"],
                                 "effect": g.effect, "title": title})

    # -- the fold (V5)

    async def _fold(self, session) -> None:
        """Fold the oldest whole turns when the window is over budget; a failed fold retries after the next turn."""
        view = self._view(session)
        upto = assemble.fold_point(self.db, view)
        if upto is None:
            return
        ev = {"session": session, "origin": "system", "clone": self.id}
        try:
            resp = await self.router.complete(assemble.fold_request(self.db, view, upto))
        except LLMError as e:
            self._errors(ev, 0, _failed(e))
            return
        if resp.stop_reason == "interrupted" and asyncio.current_task().cancelling():
            raise asyncio.CancelledError  # a shutdown mid-fold: the adapter returned, no half summary
        self._errors(ev, 0, resp.fallbacks)
        text = "".join(b["text"] for b in resp.blocks if b["type"] == "text")
        if resp.stop_reason == "refusal" or not text.strip():  # a failed fold: the window stays as it is
            self._errors(ev, 0, [{"model": resp.model, "status": 0,
                                  "message": f"fold failed: the summarize step ended {resp.stop_reason} without text"}])
            return
        lines = assemble.handle_lines(self.db, view, upto)
        prev = self.db.last_fold(self.id, session)
        first = next(r["seq"] for r in self.db.context(self.id, session, prev["upto"] if prev else 0)
                     if r["type"] != "summary")
        self.record("summary", **ev, first=first, upto=upto, text=text + "\n" + "\n".join(lines),
                    handles=[line.split()[0] for line in lines], model=resp.model, usage=resp.usage)

    # -- gates (V22): handed off, decided, reported

    def decide(self, gate: int, decision: str, by: str, scope: str = "", grade: str = "") -> bool:
        """`approved` or `denied`, once: records `resume`; an approval runs the call (a task on this loop); the
        report item is queued when the outcome is recorded. An approval may also carry (Q5) `scope` - `tool` or
        `server` grants the tool, or every tool of its MCP server, "allow" in config; `session` is recorded on
        `resume` for `state.session_grants` - and (Q3) `grade`, the owner's grade for an unknown gate's missed
        lookup, written into the effect map: `effect_by.<arg>.<value>`, or `effect` when the tool had no map
        and no annotations; a call that lacked the map's argument has no value to key on, so only `resume`
        holds that grade. False, with nothing recorded, for an unknown or already decided gate, any other
        decision, a scope on a destructive, external_send or unknown gate, `server` on a native tool, or a
        grade that is not a grade or is for a gate that was not unknown."""
        row = self.db.gate(gate)
        if decision not in ("approved", "denied") or row is None or row["clone"] != self.id or row["decision"] is not None:
            return False
        _, server, tool = row["tool"].split("__", 2) if row["tool"].startswith("mcp__") else ("", "", "")
        if ((scope or grade) and decision != "approved" or scope not in SCOPES or scope and row["effect"] in FLOOR
                or scope == "server" and not server
                or grade and (grade not in config.GRADES or row["effect"] != "unknown")):
            return False
        cfg = config.load(self.home)
        if scope in ("tool", "server"):
            key = row["tool"] if scope == "tool" else f"mcp__{server}__*"
            config.set(self.home, "grants", {**cfg["grants"], key: "allow"})
            self.record("config_set", key="grants")  # what `config.set(..., ledger=)` records: the key, never the value
        missed = self.db.context(self.id, row["session"], gate - 1)[0].get("unknown") if grade else None  # the gate's
        if grade and (missed is None or isinstance(missed.get("value"), str)):
            entry = cfg["tools"].get(server, {}).get(tool, {})
            entry = {"effect": grade} if missed is None else {"effect_by": {missed["arg"]: {
                **entry.get("effect_by", {}).get(missed["arg"], {}), missed["value"]: grade}}}
            config.set(self.home, "tools", {**cfg["tools"], server: {**cfg["tools"].get(server, {}), tool: entry}})
            self.record("config_set", key="tools")
        self.record("resume", session=row["session"], turn=row["turn"], origin="user", trust="user", clone=self.id,
                    gate=gate, decision=decision, by=by, **({"scope": scope} if scope else {}),
                    **({"grade": grade} if grade else {}))
        if decision == "approved":
            task = asyncio.get_running_loop().create_task(self._run_gate(row))
            self._calls.add(task)
            task.add_done_callback(self._calls.discard)
        else:
            self._report(gate)
        return True

    async def _run_gate(self, gate) -> None:
        """The approved call, exactly once, through `Broker(approved=...)`; an interrupt while it runs makes its
        outcome unknown. A shutdown records nothing (restore writes "outcome unknown")."""
        event = threading.Event()
        self._events.add(event)
        broker = Broker(dict(tools.TOOLS), "user", config.load(self.home), session_grants=set,
                        control=SimpleNamespace(interrupt=event, session=gate["session"]),
                        approved={gate["call_id"]: (gate["tool"], gate["args"])})
        try:
            out = await asyncio.to_thread(broker.call, gate["call_id"], gate["tool"], gate["args"])
        except Exception as e:  # past the handler's own catch (coerce, normalize): the gate still gets an outcome
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            self._events.discard(event)
        ok = bool(out["ok"]) and not event.is_set()
        body = USER_UNKNOWN if event.is_set() else out["result"] if ok else out["error"]
        self._result({"session": gate["session"], "turn": gate["turn"], "origin": "user", "clone": self.id}, 0,
                     gate["call_id"], gate["tool"], ok, body, gate=gate["seq"])
        self._report(gate["seq"])

    def _report(self, seq) -> None:
        gate = self.db.gate(seq)
        self.enqueue(Item("continue", self._report_line(gate), gate["session"], gate=seq))

    def _report_line(self, gate) -> str:
        head = f"gate {gate['seq']} ({gate['tool']})"
        if gate["decision"] != "approved":
            return f"{head} denied by the user"
        # ponytail: reads the session's context from the result on; a by-seq context reader if sessions grow long
        row = self.db.context(gate["clone"], gate["session"], gate["result"] - 1)[0]
        rendered = results.render({**row, "body": self.db.result(gate["result"])["body"]})
        return f"{head} approved → {rendered}" if row["ok"] else f"{head} approved → failed: {rendered}"

    def _reports(self, item) -> list[dict]:
        """The gates one report turn reports: every decided gate of this clone and session whose outcome is
        recorded and not yet reported, in gate order (queued report items of one session merge). [] when the
        item's own gate was already reported by an earlier merged turn."""
        own = self.db.gate(item.gate)
        if own is None or own["reported"]:
            return []
        return [g for g in self.db.open_gates() if g["clone"] == self.id and g["session"] == item.session
                and (g["decision"] == "denied" or g["decision"] == "approved" and g["result"] is not None)]

    # -- restart (V12)

    def restore(self) -> None:
        """At daemon start, after `state.catch_up`: spawn the clone once per home; close a turn a dead daemon left
        running (nothing re-run); re-queue decided gates never reported, an approved one without a result first
        marked "outcome unknown" (never re-run). Open gates need nothing: their rows are already listed."""
        self._start = self.db.cursor
        row = self.db.clone_row(self.id)
        if row is None:
            self.record("spawn", clone=self.id, parent="", preset=self.role)
        elif row["state"] == "running":
            ev = {"session": row["session"], "turn": row["turn"], "origin": "system", "clone": self.id}
            fold = self.db.last_fold(self.id, row["session"])
            steps, _, usage = _spent(self._rows(row["session"], row["turn"], fold["upto"] if fold else 0))
            self.record("interrupted", **ev, step=steps, why="restart")
            self.record("turn_end", **ev, kind="interrupted", steps=steps, usage=usage)
        for gate in self.db.open_gates():
            if gate["clone"] != self.id or gate["decision"] is None:
                continue
            if gate["decision"] == "approved" and gate["result"] is None:
                self._result({"session": gate["session"], "turn": gate["turn"], "origin": "system", "clone": self.id},
                             0, gate["call_id"], gate["tool"], False, assemble.RESTART_UNKNOWN, gate=gate["seq"])
            self._report(gate["seq"])
