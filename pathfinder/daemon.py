"""The resident process: one unix socket, sessions, one lane per session.

Every fact is appended to the ledger first and projected into the state from that same
event, so deleting state.db loses nothing and live writes cannot drift from a rebuild.
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

from . import clock, config, jobs, keychain, loop, memory, rules as rules_mod, signals, tools
from .broker import Broker, Gate, chat_approval
from .deliver import Notice, Outbox
from .ledger import Ledger
from .llm import LLMError, resolve
from .llm import slot as slot_mod
from .loop import TurnControl
from . import installer as installer_mod
from .mcp import attach as mcp_attach
from .state import State

SOCKET = "pathfinder.sock"
PROMPT = Path(__file__).parent / "prompts" / "system.txt"
LINE_LIMIT = 1 << 20  # one NDJSON line; asyncio's 64 KiB default is too small for a pasted file
SIGNAL_TURN_BUDGET = 4  # D20: a signal turn is a few tool calls plus a sentence, not a conversation
PUSH_FIELDS_LIMIT = 4096  # a pushed signal's `fields` text goes verbatim into the envelope


def default_step_factory(cfg, control, tools_for_origin):
    """Resolved per turn, so an unset model surfaces as that turn's error. `tools_for_origin`
    is what the model is shown - D25 priority "user" (jobs.default_job_step_factory: "job")."""
    endpoint = resolve(cfg, cfg["model"])
    schemas = [spec.schema() for spec in tools_for_origin.values()]
    return slot_mod.stream_step(endpoint, schemas, control, "user")


class _Lane:
    """A session's waiting queue: ("say", text) | ("resume", approval_id) | ("signal", Signal).
    Plain `asyncio.Queue` has no peek, and a signal bundle needs one: when several signals
    arrive back to back, the daemon drains every consecutive `signal` item behind the one it
    just popped into one turn, without disturbing anything queued after them."""

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

    def empty(self) -> bool:
        return not self._items


class Session:
    def __init__(self, sid):
        self.id = sid
        self.queue = _Lane()  # the lane: ("say", text) | ("resume", approval_id) | ("signal", Signal)
        self.conns = set()
        self.worker = None
        self.control: TurnControl | None = None  # of the running turn; None between turns
        self.turn = ""  # id of the running turn, valid while control is not None
        self.origin = None  # "user" | "signal" of the running turn, set with control (D17)
        self.last_cwd = None  # attach()'s last pushed cwd - dedupes a client reattaching every second


class Daemon:
    def __init__(self, home, step_factory=None, job_step_factory=None, pass_step_factory=None):
        self.home = Path(home)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.state.catch_up(self.ledger)  # a deleted or stale DB heals itself here
        self.step_factory = step_factory or default_step_factory
        self.job_step_factory = job_step_factory or jobs.default_job_step_factory
        self.pass_step_factory = pass_step_factory or memory.default_pass_step_factory
        # Byte-stable across every turn, user and signal alike (cache): the Signal-turns clause
        # is part of the resident prompt itself, never appended per-turn. Only the local-time
        # line (added separately in turn()/_signal_model_turn) changes turn to turn.
        self.system_prompt = PROMPT.read_text(encoding="utf-8") + "\n\n" + signals.SIGNAL_TURNS_CLAUSE
        self.sessions = {}
        self.writers = set()  # every open connection, attached or not: close() closes them all
        self.server = None
        self.dispatch = lambda fn: fn()  # direct until start() swaps in the threadsafe version
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
        self.jobs_mgr = jobs.Manager(self.home, self.record, self.push_signal, self.job_step_factory, self.rules,
                                     send=self._send_all, job_state=self.state.job)
        jobs.bind(self.jobs_mgr)
        self.memory_mgr = memory.Manager(self.home, self.record, self.push_signal, self.pass_step_factory,
                                         self.rules, list(self.senses.values()), is_busy=self._is_busy)
        # `Manager.apply` runs on a worker thread (the pass thread or a user turn's own thread);
        # `record` is loop-thread-only, so marshal through `dispatch` like every other callback.
        self.memory_mgr.on_rules_changed = lambda: self.memory_mgr.dispatch(self.reload_rules)
        memory.bind(self.memory_mgr)
        # A row with empty `messages` is a proposal (Manager.edit's/Attach.register's shape),
        # keyed by tool.
        self.PROPOSALS = {"edit_memory": self.memory_mgr.decide, "accept_pins": self.mcp.accept_pins,
                          "install_server": self.installer.decide}

    def _is_busy(self) -> bool:
        """The memory trigger's own gate: no turn running in any session, no job holding the
        slot. `running` only - a job waiting days at a gate holds nothing. Called from the
        trigger's own thread: `list(...)` against a dict the loop thread may be mutating."""
        return (any(s.control is not None for s in list(self.sessions.values()))
                or any(j.state == "running" for j in self.jobs_mgr.jobs.values()))

    def reload_rules(self) -> None:
        """Re-parses RULES.md and rebinds it everywhere it's held. Called after an approved
        RULES.md edit (`memory.Manager.apply`'s `on_rules_changed`) and from `run_signal_turn`
        when a hand edit moved the file's mtime - a parse error keeps the old rules. The mtime
        is recorded either way: a broken file is retried only after the next edit, not every
        signal turn."""
        self._rules_mtime = (self.home / "RULES.md").stat().st_mtime
        try:
            rules = rules_mod.load(self.home, list(self.senses.values()))
        except ValueError as e:
            self.record("error", origin="system", message=f"RULES.md: {e}"[:300])
            return
        self.rules = rules
        self.outbox.rules = rules
        self.jobs_mgr.rules = rules
        self.memory_mgr.rules = rules

    # -- recording: ledger first, then the same event into the state. Loop thread only:
    #    the SQLite connection belongs to it, and turns run in worker threads.
    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        if type == "approval_pending" and fields.get("origin") == "user" and not fields.get("messages"):
            # a proposal from the user's own turn owes a Gate's line (live: "approve" typed into chat);
            # its args are the recorded ones, so the screen shows what the approval holds (D53)
            args = installer_mod.brief(event["args"]) if fields["tool"] == "install_server" else event["args"]
            line = {"type": "gate", "id": fields["id"], "tool": fields["tool"], "args": args}
            self.dispatch(lambda: self._send_to_session(fields.get("session"), line))
        if type == "user" and (pasted := keychain.handles(event)):  # D53: the owner sees where the token went
            self._send_to_session(fields.get("session"), {"type": "pasted", "handles": pasted})
        if type == "install_failed" and fields.get("names"):  # D40 stopped on a missing secret: the screens ask
            self._ask_secret(fields["names"][0], self.installer._server_id(fields["name"]), fields["name"])
        return event

    def _ask_secret(self, name, server_id="", server="", writer=None) -> None:
        """D53: the harness asks every screen (or the one attaching) for a missing secret, typed masked."""
        for w in [writer] if writer else [w for s in self.sessions.values() for w in s.conns if not w.is_closing()]:
            _write(w, {"type": "secret.ask", "server_id": server_id, "name": name, "server": server})

    # -- socket ------------------------------------------------------------------------
    async def start(self):
        # From here, jobs.Manager marshals record()/push() onto this loop thread - state.apply
        # and a socket write are this thread's alone, and a job calls from a worker thread.
        aio = asyncio.get_running_loop()

        def dispatch(fn):
            try:
                aio.call_soon_threadsafe(fn)
            except RuntimeError:
                pass  # loop already closed: a job's late call has nothing left to report to
        self.jobs_mgr.dispatch = dispatch
        self.memory_mgr.dispatch = dispatch
        self.mcp.dispatch = dispatch
        self.installer.dispatch = dispatch
        self.dispatch = dispatch
        self.jobs_mgr.sweep()  # a job the last daemon died under is closed and announced here
        path = self.home / SOCKET
        if path.exists():
            if _answers(path):
                raise RuntimeError(f"a daemon is already serving {path}")
            path.unlink()  # left behind by a crash
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
        return self.server

    async def serve(self):
        await self.start()
        self.sense_loop.start()
        self.memory_mgr.start()
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
        self.jobs_mgr.dispatch = lambda fn: None  # drop a job's late calls, not race a closing state
        self.memory_mgr.dispatch = lambda fn: None
        self.mcp.dispatch = lambda fn: None
        self.installer.dispatch = lambda fn: None
        self.dispatch = lambda fn: None
        if self.mcp.loaded is not None:
            await self.mcp.loaded
        await self.sense_loop.stop()
        for s in self.sessions.values():
            if s.worker:
                s.worker.cancel()
        for w in list(self.writers):
            w.close()  # wait_closed() below waits for every connection, a push sense's too (a hook's `signal`)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        (self.home / SOCKET).unlink(missing_ok=True)
        self.jobs_mgr.close()
        self.memory_mgr.stop()
        self.memory_mgr.close()
        self.mcp.close()
        self.state.close()

    def _sense_error(self, name, exc):
        try:
            self.record("error", origin="system", message=f"sense {name}: {type(exc).__name__}: {exc}"[:300])
        except Exception:
            pass  # the ledger itself failing must not take a poll loop down with it

    def _is_running(self, sid) -> bool:
        s = self.sessions.get(sid)
        return s is not None and s.control is not None

    def _is_attached(self, sid) -> bool:
        s = self.sessions.get(sid)
        return bool(s and s.conns)

    def _send_to_session(self, sid, msg) -> None:
        """Outbox knows sessions only by id (it never touches daemon internals) - resolve it
        here before handing off to `send`, which wants the Session object."""
        s = self.sessions.get(sid)
        if s is not None:
            self.send(s, msg)

    def _send_all(self, msg) -> None:  # every screen: D41 may have moved one away from the job's session
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
                    if session is None or (session.control is None and self.state.latest_session(
                            config.load(self.home)["session_resume_s"]) != session.id):  # D41: over after 2 h quiet
                        session = self.attach(writer, None, session)
                    if session.control is not None and session.origin == "user":
                        self.steer(session, text)  # the user's own turn is running: a mid-turn steer
                    else:  # no turn running, or a signal turn: queued as its own turn (D17)
                        session.queue.put_nowait(("say", text))
                elif kind == "interrupt":
                    if session is None or session.control is None:
                        _write(writer, {"type": "error", "message": "no turn is running"})
                        continue
                    session.control.interrupt.set()
                elif kind in ("approve", "deny"):
                    approval_id = msg.get("id")
                    if not isinstance(approval_id, str) or not approval_id:
                        _write(writer, {"type": "error", "message": f"{kind} needs an \"id\""})
                        continue
                    row = self.state.approval(approval_id)
                    if row is None or row["decision"] is not None:
                        _write(writer, {"type": "error", "message": f"no pending approval: {approval_id}"})
                        continue
                    decision = "approved" if kind == "approve" else "denied"
                    if row.get("job"):  # a job's own thread (D24), not a session turn
                        self.record("approval", session=row["session"], turn=row["turn"], origin="user",
                                    id=approval_id, decision=decision, job=row["job"])
                        self.jobs_mgr.resume(approval_id, decision)
                        _write(writer, {"type": "turn.end", "turn": "", "kind": "resumed"})
                        continue
                    # A queued proposal (empty `messages` - Manager.edit's and Attach.register's
                    # shape) routes by tool through PROPOSALS; a broker Gate (non-empty messages,
                    # the model's own call id) is a LANE row and must resume the turn instead.
                    if row["tool"] in self.PROPOSALS and not row.get("job") and not row["messages"]:
                        self.record("approval", session=row["session"], turn=row["turn"], origin="user",
                                    id=approval_id, decision=decision)
                        result = self.PROPOSALS[row["tool"]](approval_id, decision)
                        if inspect.isawaitable(result):  # an async handler (mcp.accept_pins) blocks nothing
                            await result
                        _write(writer, {"type": "turn.end", "turn": "", "kind": "resumed"})
                        continue
                    target = self._session(row["session"])
                    if session is not target:
                        if session:
                            session.conns.discard(writer)
                        target.conns.add(writer)
                        session = target
                    self.record("approval", session=row["session"], turn=row["turn"], origin="user",
                                id=approval_id, decision=decision)
                    session.queue.put_nowait(("resume", approval_id))
                elif kind == "job.stop":
                    id = msg.get("id")
                    if not isinstance(id, str) or not id:
                        _write(writer, {"type": "error", "message": "job.stop needs an \"id\""})
                        continue
                    self.jobs_mgr.stop(id)
                elif kind == "job.steer":
                    id, text = msg.get("id"), msg.get("text")
                    if not isinstance(id, str) or not id or not isinstance(text, str) or not text.strip():
                        _write(writer, {"type": "error", "message": "job.steer needs an \"id\" and non-empty \"text\""})
                        continue
                    self.jobs_mgr.steer(id, text)
                elif kind == "memory.run":
                    result = await asyncio.to_thread(self.memory_mgr.run_pass, True)
                    _write(writer, {"type": "memory.run", "result": result})
                elif kind in ("install", "uninstall", "secret"):  # a secret's value is stored, never recorded (D53)
                    _write(writer, await self.installer.handle(msg, session.id if session else ""))
                elif kind == "signal":
                    sense_name, fields = msg.get("sense"), msg.get("fields")
                    sense = self.senses.get(sense_name)
                    if sense is None or not sense.push_capable or not isinstance(fields, dict):
                        _write(writer, {"type": "error", "message": f"unknown or non-push sense: {sense_name!r}"})
                        continue
                    if len(json.dumps(fields, ensure_ascii=False)) > PUSH_FIELDS_LIMIT:
                        _write(writer, {"type": "error", "message": "fields too large (4 KB limit)"})
                        continue
                    try:
                        signal = sense.module.build(fields)
                    except Exception as e:  # the only untrusted local input - never take the daemon down
                        _write(writer, {"type": "error", "message": f"bad fields for {sense_name}: {e}"})
                        continue
                    self.push_signal(signal)
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
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid)
            s.worker = asyncio.create_task(self.lane(s))
        return s

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
        return s

    def send(self, s, msg):
        for w in list(s.conns):
            if w.is_closing():
                s.conns.discard(w)
            else:
                _write(w, msg)

    def steer(self, s, text):
        event = self.record("user", session=s.id, turn=s.turn, origin="user", trust="user", text=text, steer=True)
        s.control.steers.append(event["text"])  # as recorded: a pasted token rides on as its handle (D53)
        if not s.control.in_tool:  # the daemon does not cut a stream while a tool handler is running
            s.control.steer_cut.set()

    async def _second_system_message(self) -> str:
        """`clock.now_line()`, the senses' agenda (D42), then USER.md's body and the wiki map when there
        are any (partition 5) - never in `self.system_prompt`, which stays byte-stable (D20's cache)."""
        agenda = await asyncio.to_thread(signals.context, list(self.senses.values()))
        return "\n\n".join(p for p in (clock.now_line(), agenda, memory.context(self.home, self.state)) if p)

    # -- turns -------------------------------------------------------------------------
    async def lane(self, s):
        while True:
            item_kind, payload = await s.queue.get()
            if item_kind == "signal":
                await self.run_signal_turn(s, [payload] + s.queue.drain_signals())
                continue
            if item_kind == "signal_ready":
                # a `speak` set (notice_id, to_speak, summaries) whose rate-limit hold has (or may
                # have) elapsed - re-check rather than re-triage: already decided and recorded.
                wait = signals.hold_for(self.state, self.rules)
                if wait > 0:
                    asyncio.get_running_loop().call_later(wait, s.queue.put_nowait, ("signal_ready", payload))
                else:
                    await self._signal_model_turn(s, *payload)
                continue
            turn = uuid.uuid4().hex[:12]
            leftover = []
            try:
                if item_kind == "say":
                    kind = await self.turn(s, turn, payload)
                elif item_kind == "resume":
                    kind = await self.resume_turn(s, turn, payload)
                else:  # "continue": answer steers that arrived after the model's last chunk
                    kind = await self.turn(s, turn, None, trailing=payload)
            except Exception as e:  # a bug in one turn must not end the session's lane
                kind = "error"
                self.send(s, {"type": "error", "message": f"internal error: {type(e).__name__}"})
                try:
                    self.record("error", session=s.id, turn=turn, origin="user", message=f"{type(e).__name__}: {e}"[:300])
                    self.record("turn_end", session=s.id, turn=turn, origin="user", kind=kind)
                except Exception:
                    pass  # the ledger itself is failing (disk full?): a dead lane would turn every later `say` into silence
            finally:
                if s.control is not None:
                    leftover = list(s.control.steers)  # said after the last drain: recorded, not yet answered
                    s.control, s.turn, s.origin = None, "", None
            self.send(s, {"type": "turn.end", "turn": turn, "kind": kind})
            self.outbox.release()  # a held notice may be releasable now that this turn ended
            if leftover and kind != "gated":  # a gate took its leftovers into the pending messages
                s.queue.put_nowait(("continue", "\n".join(leftover)))

    async def turn(self, s, turn, text, trailing=None):
        """`text` is a new user message; `trailing` is steer text already in the ledger that no
        model call has seen yet (it arrived after the last drain of the previous turn)."""
        reply = text is not None and chat_approval(  # D52: before this user line is recorded
            self.state, s.id, text, config.load(self.home)["session_resume_s"])
        if text is not None:
            self.record("user", session=s.id, turn=turn, origin="user", trust="user", text=text)
        self.record("turn_start", session=s.id, turn=turn, origin="user")
        if reply:  # the harness answers, no model request; the approval stays pending
            self.send(s, {"type": "turn.start", "turn": turn})
            self.send(s, {"type": "delta", "text": reply})
            self.record("assistant", session=s.id, turn=turn, origin="user", text=reply, final=True)
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="answer")
            return "answer"
        messages = [{"role": "system", "content": self.system_prompt},
                   {"role": "system", "content": await self._second_system_message()}] + self.state.history(s.id)
        if trailing:
            messages.append({"role": "user", "content": trailing})
        return await self._run_turn(s, turn, messages, resume=False)

    # -- signals (D20) -------------------------------------------------------------------
    def push_signal(self, signal) -> None:
        """The push door: a hook client's `signal`, a poll sense's SenseLoop, and `jobs.Manager`
        (direct, never the socket) all land here. `state.seen` is checked against disk *before*
        this call, so only a repeat of the same key within 24 h is silenced."""
        now = time.time()
        dup = signals.seen_recently(self.state, signal.key, now)
        self.record("signal", origin="signal", trust=signal.trust, sense=signal.sense,
                    kind=signal.kind, key=signal.key, summary=signal.summary, fields=signal.fields,
                    exempt=signal.exempt)
        if dup:
            self.record("decision", origin="signal", signal_key=signal.key, action="silence", why="seen")
            return
        target = self._target_session()
        target.queue.put_nowait(("signal", signal))

    def _target_session(self) -> Session:
        cfg = config.load(self.home)
        sid = self.state.latest_session(cfg["session_resume_s"])
        if sid is None:
            sid = uuid.uuid4().hex[:12]
            self.record("session_start", session=sid)
        s = self._session(sid)
        for old in [x for x in self.sessions.values() if x is not s and x.control is None]:
            s.conns, old.conns = s.conns | old.conns, set()  # D41: a screen left open follows the notice
        return s

    async def run_signal_turn(self, s, bundle) -> None:
        """Triage the whole bundle through rules.decide (D8) FIRST, unconditionally: a
        defer/silence/ask_model result is recorded and done for free. Only the resulting
        `speak` set is subject to the rate-limit hold, and that hold never blocks the lane
        (`signal_ready` + `call_later`, not a sleep here) - the reviewer's blocker was a
        silenced signal freezing the next `say` for up to min_interval_s."""
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
            # turn ends - a bundle of 5 with daily_budget=1 must not speak all 5.
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
        # The text here is the summaries only; _signal_model_turn's own notice may say more,
        # but that richer text reaches the ledger through `delivery`/`assistant`, not this row.
        notice_id = uuid.uuid4().hex[:12]
        keys = [sig.key for sig, _why in to_speak]
        # D62: every sense's summary reaches the screen; cleaned once, so the live notice, the one
        # rebuilt after a restart (`undelivered`, from this row), and history hold the same text.
        summaries = tools.clean("\n".join(sig.summary for sig, _why in to_speak))
        for sig, why in to_speak:
            self.record("decision", session=s.id, origin="signal", signal_key=sig.key, action="speak",
                        why=why, notice_id=notice_id, text=summaries, signal_keys=keys)
        wait = signals.hold_for(self.state, self.rules)
        if wait > 0:
            asyncio.get_running_loop().call_later(
                wait, s.queue.put_nowait, ("signal_ready", (notice_id, to_speak, summaries)))
        else:
            await self._signal_model_turn(s, notice_id, to_speak, summaries)

    async def _signal_model_turn(self, s, notice_id, to_speak, summaries) -> None:
        """D20: origin signal, no history, budget 4. Delivered text is the sense summaries
        joined, plus the model's one or two sentences when it answers - the summaries alone on
        a gate, an LLMError, or an empty answer. `notice_id`, the clean `summaries` and the
        `decision speak` rows for `to_speak` came from the caller, before any rate-limit hold."""
        turn = uuid.uuid4().hex[:12]
        sigs = [sig for sig, _why in to_speak]
        self.record("turn_start", session=s.id, turn=turn, origin="signal")
        control = TurnControl()
        s.control, s.turn, s.origin = control, turn, "signal"  # note 13: without this, is_running(s) is False mid-turn
        answer, gated, ending = "", False, {"kind": "answer"}
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "system", "content": await self._second_system_message()},
                {"role": "user", "content": signals.render_envelope(sigs)},  # never recorded as `user`
            ]
            cfg = config.load(self.home)
            aio = asyncio.get_running_loop()
            out_q = asyncio.Queue()

            def emit(kind, payload):
                aio.call_soon_threadsafe(out_q.put_nowait, (kind, payload))

            if self.mcp.loaded is not None and not self.mcp.loaded.done():
                await self.mcp.loaded  # a turn must see the full tool surface, not a partial one
            signal_tools = tools.for_origin("signal")
            broker = Broker(signal_tools, "signal", cfg["grants"], control=control, emit=emit)
            step = self.step_factory(cfg, control, signal_tools)

            def work():
                try:
                    return loop.run(step, messages, call_tool=broker.call, emit=emit,
                                    budget=SIGNAL_TURN_BUDGET, control=control,
                                    final_step=self.step_factory(cfg, control, {}))
                finally:
                    aio.call_soon_threadsafe(out_q.put_nowait, (None, None))

            try:
                running = asyncio.create_task(asyncio.to_thread(work))
                while True:
                    kind, payload = await out_q.get()
                    if kind is None:
                        break
                    if kind == "tool_call":
                        recorded = {**payload, "args": tools.clip_for_ledger(payload["args"])}
                        self.record("tool_call", session=s.id, turn=turn, origin="signal", **recorded)
                    elif kind == "tool_result":
                        recorded = {**payload, "result": tools.clip_for_ledger(payload["result"])}
                        self.record("tool_result", session=s.id, turn=turn, origin="signal", **recorded)
                out = await running
                answer = out.text
                ending = {"kind": out.kind, "steps": out.steps, "usage": out.usage}
            except Gate:
                # A signal turn has no history to resume into and policy already asks for every
                # write/network call from origin "signal" - the attempt is dropped, not approved
                # later; the notice still goes out with the sense's own summary.
                gated, ending = True, {"kind": "gated"}
            except LLMError as e:
                # summaries alone (D20's own fallback); the blocked call is a fact worth one line
                self.record("error", session=s.id, turn=turn, origin="signal", status=e.status, message=str(e)[:300])
                ending = {"kind": "error"}
        finally:
            s.control, s.turn, s.origin = None, "", None

        text = f"{summaries}\n{answer}".strip() if answer and not gated else summaries
        keys = [sig.key for sig in sigs]
        self.record("turn_end", session=s.id, turn=turn, origin="signal", **ending)
        self.outbox.enqueue(Notice(id=notice_id, session=s.id, text=text, why=to_speak[0][1], signal_keys=keys))

    async def resume_turn(self, s, turn, approval_id):
        row = self.state.approval(approval_id)
        self.record("turn_start", session=s.id, turn=turn, origin="user")
        approved = {row["call_id"]: (row["tool"], row["args"])} if row["decision"] == "approved" else {}
        denied = {row["call_id"]} if row["decision"] == "denied" else frozenset()
        return await self._run_turn(s, turn, row["messages"], resume=True, approved=approved, denied=denied)

    async def _run_turn(self, s, turn, messages, *, resume, approved=None, denied=frozenset()):
        self.send(s, {"type": "turn.start", "turn": turn})
        control = TurnControl()
        control.session = s.id  # the one way start_job's handler learns which session to report to
        s.control, s.turn, s.origin = control, turn, "user"  # resume/continue are user turns too (D17)

        aio = asyncio.get_running_loop()
        out_q = asyncio.Queue()

        def emit(kind, payload):  # called from the worker thread
            aio.call_soon_threadsafe(out_q.put_nowait, (kind, payload))

        cfg = config.load(self.home)
        if self.mcp.loaded is not None and not self.mcp.loaded.done():
            await self.mcp.loaded  # a turn must see the full tool surface, not a partial one
        user_tools = tools.for_origin("user")
        broker = Broker(user_tools, "user", cfg["grants"], control=control, emit=emit,
                        approved=approved, denied=denied)

        def work(step, budget):
            try:
                return loop.run(step, messages, call_tool=broker.call, emit=emit,
                                budget=budget, control=control, resume=resume, second_look=list(user_tools),
                                final_step=self.step_factory(cfg, control, {}))  # D30: no tools
            finally:
                aio.call_soon_threadsafe(out_q.put_nowait, (None, None))

        try:
            running = asyncio.create_task(
                asyncio.to_thread(work, self.step_factory(cfg, control, user_tools), cfg["max_steps"]))
            while True:
                kind, payload = await out_q.get()
                if kind is None:
                    break
                if kind == "delta":
                    self.send(s, {"type": "delta", "text": payload})
                elif kind == "tool_call":
                    recorded = {**payload, "args": tools.clip_for_ledger(payload["args"])}
                    self.record("tool_call", session=s.id, turn=turn, origin="user", **recorded)
                elif kind == "tool_result":
                    recorded = {**payload, "result": tools.clip_for_ledger(payload["result"])}
                    self.record("tool_result", session=s.id, turn=turn, origin="user", **recorded)
                    self.send(s, {"type": "tool", "name": payload["name"], "ok": payload["ok"]})
                elif kind == "retract":  # D51: kept in the ledger as the owner saw it, never in history
                    self.record("assistant", session=s.id, turn=turn, origin="user", text=payload["draft"],
                                final=False, withdrawn={"flags": payload["flags"], "why": payload["why"]})
                    self.send(s, {"type": "retract", "what": payload["what"], "why": payload["why"]})
            out = await running
        except Gate as g:
            # The approval has its own id: the tool-call id is a string the model chose and
            # re-uses across turns, so it cannot be the key. Steers said after the last drain go
            # into the pending messages, or the resume would never see them.
            if g.said:
                self.record("assistant", session=s.id, turn=turn, origin="user", text=g.said, final=True)
            loop._drain(control, messages, envelope=False)
            approval_id = uuid.uuid4().hex[:12]
            pending = self.record("approval_pending", session=s.id, turn=turn, origin="user", id=approval_id,
                                  call_id=g.id, tool=g.name, args=g.arguments, messages=messages)
            self.send(s, {"type": "gate", "id": approval_id, "tool": g.name, "args": pending["args"]})
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="gated", check=g.check)
            return "gated"
        except LLMError as e:
            # Out of credit, rate limited, offline, no model set: the assistant goes quiet,
            # the daemon does not die. Signals keep landing in the ledger.
            if getattr(e, "said", ""):  # Invariant 2: what streamed before the error reaches the ledger
                self.record("assistant", session=s.id, turn=turn, origin="user", text=e.said, final=True)
            self.record("error", session=s.id, turn=turn, origin="user", status=e.status, message=str(e)[:300])
            self.send(s, {"type": "error", "message": str(e)[:300]})
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="error",
                        check=getattr(e, "check", None))
            return "error"

        if out.kind == "cancelled":
            self.record("interrupted", session=s.id, turn=turn, origin="user")
        if out.text:
            self.record("assistant", session=s.id, turn=turn, origin="user", text=out.text, final=True)
        self.record("turn_end", session=s.id, turn=turn, origin="user", kind=out.kind,
                    steps=out.steps, usage=out.usage, check=out.check)
        return out.kind


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
