"""The resident process: one unix socket, sessions, and the root clone's one lane (V11).

Every fact is appended to the ledger first and projected into the state from that same
event, so deleting state.db loses nothing and live writes cannot drift from a rebuild.
The daemon makes no model request itself: the root clone does, through its router.
"""
from __future__ import annotations

import asyncio
import collections
import inspect
import json
import os
import signal
import socket
import time
import uuid
from pathlib import Path

from . import clock, config, keychain, memory, rules as rules_mod, signals, skills, tools
from .broker import chat_approval
from .clone import Clone, Item
from .deliver import Notice, Outbox
from .ledger import Ledger
from . import installer as installer_mod
from .mcp import attach as mcp_attach
from .router import Router
from .state import State

SOCKET = "pathfinder.sock"
LINE_LIMIT = 1 << 20  # one NDJSON line; asyncio's 64 KiB default is too small for a pasted file
ROOT = "root"  # the root clone's id and preset


class _Lane:
    """The root's lane: ("item", Item) | ("signal", Signal), in arrival order. Plain
    `asyncio.Queue` has no peek, and a signal bundle needs one: when several signals arrive
    back to back, the daemon drains every consecutive `signal` item behind the one it just
    popped into one triage, without disturbing anything queued after them."""

    def __init__(self):
        self._items: collections.deque = collections.deque()
        self._event = asyncio.Event()

    def put_nowait(self, item) -> None:
        self._items.append(item)
        self._event.set()

    async def get(self):
        while not self._items:
            self._event.clear()
            await self._event.wait()
        return self._items.popleft()

    def drain_signals(self) -> list:
        out = []
        while self._items and self._items[0][0] == "signal":
            out.append(self._items.popleft()[1])
        return out


class Session:
    def __init__(self, sid):
        self.id = sid
        self.conns = set()
        self.last_cwd = None  # attach()'s last pushed cwd - dedupes a client reattaching every second


class Daemon:
    def __init__(self, home, router=None):
        self.home = Path(home)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.state.catch_up(self.ledger)  # a deleted or stale DB heals itself here
        self.sessions = {}
        self.writers = set()  # every open connection, attached or not: close() closes them all
        self.server = None
        self.queue = _Lane()
        self.worker = None  # the lane's task, from start()
        self.dispatch = lambda fn: fn()  # direct until start() swaps in the threadsafe version
        # The root clone (V11): every model request of a turn goes through its router, never from here.
        self.root = Clone(ROOT, ROOT, home=self.home, state=self.state, record=self.record,
                          router=router or Router(lambda: config.load(self.home)), send=self._send_to_session,
                          enqueue=lambda item: self.queue.put_nowait(("item", item)), resident=self._resident)
        self.mcp = mcp_attach.Attach(self.home, self.state, self.record, self.push_signal, self.dispatch)
        self.installer = installer_mod.Installer(self.home, self.state, self.record, self.push_signal,
                                                 self.mcp, self.dispatch)
        installer_mod.bind(self.installer)

        cfg = config.load(self.home)
        self.senses = {s.name: s for s in signals.load(cfg, self.home)}
        self.rules = rules_mod.load(self.home, list(self.senses.values()))
        self._rules_mtime = (self.home / "RULES.md").stat().st_mtime
        self.outbox = Outbox(self.state, self.rules, is_running=self._is_running,
                             attached=self._is_attached, send=self._send_to_session, record=self.record)
        self.outbox.rebuild()
        self.sense_loop = signals.SenseLoop(list(self.senses.values()), push=self.push_signal,
                                            on_error=self._sense_error)
        self.memory_mgr = memory.Manager(self.home, self.record, list(self.senses.values()))
        # `Manager.apply` runs on a worker thread (a tool's thread);
        # `record` is loop-thread-only, so marshal through `dispatch` like every other callback.
        self.memory_mgr.on_rules_changed = lambda: self.memory_mgr.dispatch(self.reload_rules)
        memory.bind(self.memory_mgr)
        skills.bind(self.home)  # D61: copies a missing shipped skill; never touches an existing one
        # A row with empty `messages` is a proposal (Manager.edit's/Attach.register's shape),
        # keyed by tool.
        self.PROPOSALS = {"edit_memory": self.memory_mgr.decide, "accept_pins": self.mcp.accept_pins,
                          "install_server": self.installer.decide}

    def reload_rules(self) -> None:
        """Re-parses RULES.md and rebinds it everywhere it's held. Called after an approved
        RULES.md edit (`memory.Manager.apply`'s `on_rules_changed`) and from `triage` when a
        hand edit moved the file's mtime - a parse error keeps the old rules. The mtime is
        recorded either way: a broken file is retried only after the next edit, not every
        triage."""
        self._rules_mtime = (self.home / "RULES.md").stat().st_mtime
        try:
            rules = rules_mod.load(self.home, list(self.senses.values()))
        except ValueError as e:
            self.record("error", origin="system", message=f"RULES.md: {e}"[:300])
            return
        self.rules = rules
        self.outbox.rules = rules

    # -- recording: ledger first, then the same event into the state. Loop thread only:
    #    the SQLite connection belongs to it, and tools run in worker threads.
    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        if type == "approval_pending" and fields.get("origin") == "user" and not fields.get("messages"):
            # a proposal from the user's own turn owes a Gate's line (live: "approve" typed into chat);
            # its args are the recorded ones, so the screen shows what the approval holds (D53); its id
            # is the event's seq, the one namespace `approve <seq>` takes
            args = installer_mod.brief(event["args"]) if fields["tool"] == "install_server" else event["args"]
            line = {"type": "gate", "id": event["seq"], "tool": fields["tool"], "args": args}
            self.dispatch(lambda: self._send_to_session(fields.get("session"), line))
        if type == "user" and (pasted := keychain.handles(event)):  # D53: the owner sees where the token went
            self._send_to_session(fields.get("session"), {"type": "pasted", "handles": pasted})
        if type == "install_failed" and fields.get("names"):  # D40 stopped on a missing secret: the screens ask
            self._ask_secret(fields["names"][0], fields.get("server_id", ""), fields["name"])
        return event

    def _ask_secret(self, name, server_id="", server="", writer=None) -> None:
        """D53: the harness asks every screen (or the one attaching) for a missing secret, typed masked."""
        for w in [writer] if writer else [w for s in self.sessions.values() for w in s.conns if not w.is_closing()]:
            _write(w, {"type": "secret.ask", "server_id": server_id, "name": name, "server": server})

    # -- socket ------------------------------------------------------------------------
    async def start(self):
        # From here, a worker thread's record()/push() is marshalled onto this loop thread - state.apply
        # and a socket write are this thread's alone.
        aio = asyncio.get_running_loop()

        def dispatch(fn):
            try:
                aio.call_soon_threadsafe(fn)
            except RuntimeError:
                pass  # loop already closed: a late call has nothing left to report to
        self.memory_mgr.dispatch = dispatch
        self.mcp.dispatch = dispatch
        self.installer.dispatch = dispatch
        self.dispatch = dispatch
        path = self.home / SOCKET
        if path.exists():
            if _answers(path):
                raise RuntimeError(f"a daemon is already serving {path}")
            path.unlink()  # left behind by a crash
        # V12, after catch_up and only once no other daemon serves this home: a turn a dead daemon left
        # running is closed, decided gates never reported are queued again; open gates are simply listed.
        self.root.restore()
        keychain.sweep(self.ledger.pasted, time.time())  # D53: pasted items older than 24 hours go, once a start
        old = os.umask(0o177)  # the socket is born 0600: no window where another user can connect
        try:
            self.server = await asyncio.start_unix_server(self.handle, path=str(path), limit=LINE_LIMIT)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        # A dead or slow server must not delay the socket bind (and so the first `say`) by its
        # timeout - loaded as a background task once the socket is up, awaited in close() instead.
        self.mcp.loaded = asyncio.ensure_future(self.mcp.load())
        self.worker = asyncio.create_task(self.lane())
        return self.server

    async def serve(self):
        await self.start()
        self.sense_loop.start()
        self.outbox.release()  # flush anything a previous crash left pending, now that we can
        loop = asyncio.get_running_loop()
        # launchd's `service stop` sends SIGTERM: the same shutdown Ctrl-C runs below (`close()`:
        # MCP servers closed, their child processes killed) - without it every npx child is
        # orphaned (m5's bench had to killpg by hand for this).
        loop.add_signal_handler(signal.SIGTERM, self.server.close)
        try:  # not `async with self.server`: its wait_closed() waits for attached clients that only close() closes
            await self.server.serve_forever()
        except asyncio.CancelledError:
            pass  # our own close() above, or an outer cancellation (Ctrl-C) - either way, clean up below
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            await self.close()

    async def close(self):
        self.memory_mgr.dispatch = lambda fn: None  # drop late calls, not race a closing state
        self.mcp.dispatch = lambda fn: None
        self.installer.close()  # no late record; a re-proposal in flight is cancelled, not awaited
        self.dispatch = lambda fn: None
        if self.worker is not None:
            # V12: a shutdown is not the clone's own interrupt, so it re-raises and records nothing;
            # the next start's restore() closes the turn as interrupted
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.mcp.loaded is not None:
            await self.mcp.loaded
        await self.sense_loop.stop()
        for w in list(self.writers):
            w.close()  # wait_closed() below waits for every connection, a push sense's too (a hook's `signal`)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        (self.home / SOCKET).unlink(missing_ok=True)
        self.memory_mgr.close()
        self.mcp.close()
        self.state.close()

    def _sense_error(self, name, exc):
        try:
            self.record("error", origin="system", message=f"sense {name}: {type(exc).__name__}: {exc}"[:300])
        except Exception:
            pass  # the ledger itself failing must not take a poll loop down with it

    def _is_running(self, sid) -> bool:
        """The root is running a turn of session `sid` (the clones row, from turn_start/turn_end)."""
        row = self.state.clone_row(self.root.id)
        return bool(row) and row["state"] == "running" and row["session"] == sid

    def _is_attached(self, sid) -> bool:
        s = self.sessions.get(sid)
        return bool(s and s.conns)

    def _send_to_session(self, sid, msg) -> None:
        """The outbox and the clone know sessions only by id (they never touch daemon internals) -
        resolve it here before handing off to `send`, which wants the Session object."""
        s = self.sessions.get(sid)
        if s is not None:
            self.send(s, msg)

    def _send_all(self, msg) -> None:  # every screen, whichever session D41 left it attached to
        for s in list(self.sessions.values()):
            self.send(s, msg)

    async def handle(self, reader, writer):
        session = None
        self.writers.add(writer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # longer than LINE_LIMIT; the stream is no longer line-aligned
                    _write(writer, {"type": "error", "message": "line too long"})
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    kind = msg["type"]
                except (ValueError, TypeError, KeyError):
                    _write(writer, {"type": "error", "message": "expected one JSON object per line with a \"type\""})
                    continue  # a bad line never costs the connection
                if session is not None and writer not in session.conns:  # D41: moved into the current session
                    session = next((x for x in self.sessions.values() if writer in x.conns), session)
                if kind == "attach":
                    session = self.attach(writer, msg.get("session"), session, msg.get("cwd"))
                elif kind == "say":
                    text = msg.get("text")
                    if not isinstance(text, str) or not text.strip():
                        _write(writer, {"type": "error", "message": "say needs a non-empty \"text\""})
                        continue
                    if session is None or (not self._is_running(session.id) and self.state.latest_session(
                            config.load(self.home)["session_resume_s"]) != session.id):  # D41: over after 2 h quiet
                        session = self.attach(writer, None, session)
                    self.say(session, text)
                elif kind == "interrupt":
                    if self.root.state != "running":
                        _write(writer, {"type": "error", "message": "no turn is running"})
                    self.root.interrupt()  # also stops an approved call still running between turns
                elif kind in ("approve", "deny"):
                    ref = msg.get("id")  # a ledger seq (`approve <seq>`), or a v1 approval id until U9
                    if not ref or isinstance(ref, bool) or not isinstance(ref, (int, str)):
                        _write(writer, {"type": "error", "message": f"{kind} needs an \"id\""})
                        continue
                    seq = ref if isinstance(ref, int) else int(ref) if ref.isdecimal() else None
                    row = self.state.approval_item(seq) if seq is not None else None
                    if row is None and (v1 := self.state.approval(str(ref))):  # an all-digit v1 id is one too
                        row = {"kind": "proposal", **v1}
                    if row is None or row["decision"] is not None:
                        _write(writer, {"type": "error", "message": f"no pending approval: {ref}"})
                        continue
                    decision = "approved" if kind == "approve" else "denied"
                    if row["kind"] == "gate":  # the root's hand-off (V22): its report is a new root turn
                        self.root.decide(seq, decision, "socket")
                        target = self._session(row["session"])
                        if session is not target:  # the report turn reaches the screen that decided
                            if session:
                                session.conns.discard(writer)
                            target.conns.add(writer)
                            session = target
                        continue
                    # A queued proposal (empty `messages` - Manager.edit's and Attach.register's
                    # shape) routes by tool through PROPOSALS; a v1 turn's gate has no turn to resume.
                    if row["tool"] not in self.PROPOSALS or row["messages"]:
                        _write(writer, {"type": "error", "message": f"no pending approval: {ref}"})
                        continue
                    self.record("approval", session=row["session"], turn=row["turn"], origin="user",
                                id=row["id"], decision=decision)
                    result = self.PROPOSALS[row["tool"]](row["id"], decision)
                    if inspect.isawaitable(result):  # an async handler (mcp.accept_pins) blocks nothing
                        await result
                    _write(writer, {"type": "turn.end", "turn": "", "kind": "resumed"})
                elif kind in ("install", "uninstall"):
                    _write(writer, await self.installer.handle(msg))
                elif kind == "secret":  # D53: stored, never recorded
                    reply = await self.installer.secret(msg, session.id if session else "")
                    if reply["type"] == "secret.stored":  # every screen hears it, so one that did not answer leaves its entry
                        self._send_all(reply)
                    else:  # an error is the sender's alone
                        _write(writer, reply)
                elif kind == "signal":  # the push door, checked and built in `signals.pushed` (D67)
                    signals.pushed(self.senses, msg, self.push_signal, lambda reply: _write(writer, reply))
                elif kind == "presence":
                    idle_s = msg.get("idle_s")
                    if (not isinstance(idle_s, (int, float)) or isinstance(idle_s, bool)
                            or not (0 <= idle_s <= 86400)):
                        _write(writer, {"type": "error", "message": "presence needs a numeric idle_s in [0, 86400]"})
                        continue
                    session = session or self.attach(writer, None, None)
                    self.record("presence", session=session.id, origin="user", idle_s=idle_s)
                    self.outbox.release()
                elif kind == "react":
                    vote = msg.get("vote")
                    if vote not in ("up", "down"):
                        _write(writer, {"type": "error", "message": "react needs \"vote\": \"up\"|\"down\""})
                        continue
                    delivery_id = msg.get("id") or self.state.latest_delivery()
                    if delivery_id is None or not self.state.has_delivery(delivery_id):
                        _write(writer, {"type": "error", "message": "no such delivery to react to"})
                        continue
                    self.record("reaction", origin="user", delivery_id=delivery_id, vote=vote)
                else:
                    _write(writer, {"type": "error", "message": f"unknown type: {kind!r}"})
        finally:
            for x in self.sessions.values():  # D41: a move may have put it in another session
                x.conns.discard(writer)
            self.writers.discard(writer)
            writer.close()

    def _session(self, sid):
        return self.sessions.setdefault(sid, Session(sid))

    def attach(self, writer, sid, current, cwd=None):
        if current:
            current.conns.discard(writer)
        if not isinstance(sid, str):
            sid = None  # a client-supplied id of any other type would crash the lookup and drop the connection
        cfg = config.load(self.home)
        known = self.state.session(sid) if sid else None
        if not known:
            sid = self.state.latest_session(cfg["session_resume_s"])
            known = self.state.session(sid) if sid else None
        if not known:
            sid = uuid.uuid4().hex[:12]
            self.record("session_start", session=sid)
        s = self._session(sid)
        s.conns.add(writer)
        if isinstance(cwd, str) and cwd != s.last_cwd:  # a client reattaching every second must not spam the ledger
            sense = self.senses.get("attach")
            if sense is not None and sense.push_capable:
                self.push_signal(sense.module.build({"cwd": cwd}))
            s.last_cwd = cwd
        _write(writer, {"type": "attached", "session": sid, "resumed": bool(known),
                        "turns": known["turns"] if known else 0})
        if "mail" in self.senses and keychain.missing(self.home, "mail"):  # U26: no screen is attached at a start
            self._ask_secret("mail", writer=writer)
        return s

    def send(self, s, msg):
        for w in list(s.conns):
            if w.is_closing():
                s.conns.discard(w)
            else:
                _write(w, msg)

    def say(self, s, text) -> None:
        """Behaviour row 1. A say to the session whose turn the root is running is a steer: the clone
        records it at its next step boundary (D17: never taken for an approval). Anything else is a new
        item, checked for an approval phrase when the lane takes it (`_run`), and whose opening `user`
        the clone records then: two says queued behind a busy lane must not land in one turn."""
        if self._is_running(s.id):
            self.root.steer(s.id, text)
        else:
            self.queue.put_nowait(("item", Item("user", text, s.id)))

    def _chat_reply(self, sid, text, reply) -> None:
        """D52: the harness answers an approval phrase (no model request; the gate stays open), recorded
        without `clone`: the screen's history and the "talked past" mark, never the root's window."""
        turn = uuid.uuid4().hex[:12]
        self.record("user", session=sid, turn=turn, origin="user", trust="user", text=text)
        self.record("turn_start", session=sid, turn=turn, origin="user")
        self._send_to_session(sid, {"type": "turn.start", "turn": turn})
        self._send_to_session(sid, {"type": "delta", "text": reply})
        self.record("assistant", session=sid, turn=turn, origin="user", text=reply, final=True)
        self.record("turn_end", session=sid, turn=turn, origin="user", kind="answer")
        self._send_to_session(sid, {"type": "turn.end", "turn": turn, "kind": "answer"})

    async def _resident(self) -> list[str]:
        """The resident block's parts, gathered once per item (P1): the local time, USER.md and the
        wiki map, the senses' agenda (D42)."""
        agenda = await asyncio.to_thread(signals.context, list(self.senses.values()))
        return [clock.now_line(), memory.context(self.home, self.state), agenda]

    # -- the lane ----------------------------------------------------------------------
    async def lane(self):
        """The root's one lane, in arrival order: items for the clone, signal bundles for triage."""
        while True:
            kind, payload = await self.queue.get()
            if kind == "signal":
                await self.triage(self._target_session(), [payload] + self.queue.drain_signals())
            else:
                await self._run(payload)

    async def _run(self, item) -> None:
        # D52, checked as the lane takes the say (as v1 did): every say queued ahead of it is recorded by now
        if item.kind == "user" and (reply := chat_approval(self.state, item.session, item.text,
                                                           config.load(self.home)["session_resume_s"])):
            self._chat_reply(item.session, item.text, reply)
            self.outbox.release()
            return
        if self.mcp.loaded is not None and not self.mcp.loaded.done():
            await self.mcp.loaded  # a turn must see the full tool surface, not a partial one
        try:
            kind = await self.root.run(item)
        except Exception as e:  # a bug in one turn must neither end the lane nor leave the root running
            kind = "error"
            self._send_to_session(item.session, {"type": "error", "message": f"internal error: {type(e).__name__}"})
            try:
                self.record("error", session=item.session, origin=item.origin, clone=self.root.id,
                            message=f"{type(e).__name__}: {e}"[:300])
                row = self.state.clone_row(self.root.id)
                if row["state"] == "running":
                    self.record("turn_end", session=row["session"], turn=row["turn"], origin=item.origin,
                                clone=self.root.id, kind=kind)
                    self._send_to_session(row["session"], {"type": "turn.end", "turn": row["turn"], "kind": kind})
            except Exception:
                pass  # the ledger itself is failing (disk full?): a dead lane would turn every later `say` into silence
        if item.kind == "schedule" and not self._is_attached(item.session):
            # no screen saw this scheduled turn: a banner says it ran; the answer stays in history
            self.outbox.enqueue(Notice(uuid.uuid4().hex[:12], item.session, f"{item.text} ({kind})", "schedule", []))
        self.outbox.release()  # a held notice may be releasable now that this turn ended

    # -- signals (D20) -------------------------------------------------------------------
    def push_signal(self, signal) -> None:
        """The push door: a hook client's `signal` and a poll sense's SenseLoop
        both land here. `state.seen` is checked against disk *before*
        this call, so only a repeat of the same key within 24 h is silenced."""
        now = time.time()
        dup = signals.seen_recently(self.state, signal.key, now)
        self.record("signal", origin="signal", trust=signal.trust, sense=signal.sense,
                    kind=signal.kind, key=signal.key, summary=signal.summary, fields=signal.fields,
                    exempt=signal.exempt)
        if dup:
            self.record("decision", origin="signal", signal_key=signal.key, action="silence", why="seen")
            return
        if signal.sense == "schedule":  # the owner's own note runs as their item: a gate it leads to is the root's
            text = f"[scheduled] {signal.fields['text']}"
            self.queue.put_nowait(("item", Item("schedule", text, self._target_session().id)))
            return
        self.queue.put_nowait(("signal", signal))

    def _target_session(self) -> Session:
        cfg = config.load(self.home)
        sid = self.state.latest_session(cfg["session_resume_s"])
        if sid is None:
            sid = uuid.uuid4().hex[:12]
            self.record("session_start", session=sid)
        s = self._session(sid)
        for old in [x for x in self.sessions.values() if x is not s and not self._is_running(x.id)]:
            s.conns, old.conns = s.conns | old.conns, set()  # D41: a screen left open follows the notice
        return s

    async def triage(self, s, bundle) -> None:
        """Triage the whole bundle through rules.decide (D8) FIRST, unconditionally: a
        defer/silence/ask_model result is recorded and done for free. The resulting `speak` set
        goes out as the senses' summaries (no signal model turn in P1; attention is P5), after
        the rate-limit hold, which never blocks the lane (`call_later`, not a sleep here)."""
        if (self.home / "RULES.md").stat().st_mtime != self._rules_mtime:  # a hand edit since load
            self.reload_rules()
        lists = rules_mod.user_lists(self.home)
        to_speak = []
        for sig in bundle:
            today = self.state.today(idle_threshold_s=self.rules.idle_threshold_s)
            action, why = rules_mod.decide(sig, self.rules, lists, today, time.time())
            if action == "ask_model":
                if today.judgments >= self.rules.judgment_cap:
                    action, why = "defer", "judgment cap"
                else:
                    speak, reason = await asyncio.to_thread(rules_mod.judge, sig, config.load(self.home))
                    self.record("judgment", session=s.id, origin="signal", signal_key=sig.key,
                                speak=speak, reason=reason)
                    action, why = ("speak" if speak else "silence"), "judged"
            # should-fix 4: the budget is spent per bundle as it is built, not only once the
            # bundle is delivered - a bundle of 5 with daily_budget=1 must not speak all 5.
            if (action == "speak" and sig.trust != "user" and not sig.exempt and self.rules.daily_budget
                    and today.spoken + len(to_speak) >= self.rules.daily_budget):
                action, why = "defer", "daily budget spent"
            if action == "speak":
                to_speak.append((sig, why))
            else:
                self.record("decision", session=s.id, origin="signal", signal_key=sig.key, action=action, why=why)
        if not to_speak:
            return
        # should-fix 5: record `decision speak` now, before the rate-limit hold - a held set
        # lives only in a call_later timer otherwise, and a stop in that window loses it
        # (the signal event has no decision, so undelivered() finds nothing to rebuild).
        keys = [sig.key for sig, _why in to_speak]
        # D62: every sense's summary reaches the screen; cleaned once, so the live notice, the one
        # rebuilt after a restart (`undelivered`, from this row), and history hold the same text.
        summaries = tools.clean("\n".join(sig.summary for sig, _why in to_speak))
        notice = Notice(id=uuid.uuid4().hex[:12], session=s.id, text=summaries, why=to_speak[0][1], signal_keys=keys)
        for sig, why in to_speak:
            self.record("decision", session=s.id, origin="signal", signal_key=sig.key, action="speak",
                        why=why, notice_id=notice.id, text=summaries, signal_keys=keys)
        self._speak(notice)

    def _speak(self, notice) -> None:
        """Into the outbox once the rate-limit hold is over, re-checked when it ends (another set may
        have spoken meanwhile) rather than re-triaged: already decided and recorded."""
        wait = signals.hold_for(self.state, self.rules)
        if wait > 0:
            asyncio.get_running_loop().call_later(wait, self._speak, notice)
        else:
            self.outbox.enqueue(notice)


def _write(writer, msg):
    writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())


def is_serving(home):
    """True when a daemon answers on this home's socket."""
    return _answers(Path(home) / SOCKET)


def _answers(path):
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(str(path))
            return True
        except OSError:
            return False
