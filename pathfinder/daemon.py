"""The resident process: one unix socket, sessions, one lane per session.

Every fact is appended to the ledger first and projected into the state from that same
event, so deleting state.db loses nothing and live writes cannot drift from a rebuild.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from pathlib import Path

from . import config, loop, tools
from .broker import Broker, Gate
from .ledger import Ledger
from .llm import LLMError, openai_compat, resolve
from .loop import TurnControl
from .state import State

SOCKET = "pathfinder.sock"
PROMPT = Path(__file__).parent / "prompts" / "system.txt"
LINE_LIMIT = 1 << 20  # one NDJSON line; asyncio's 64 KiB default is too small for a pasted file
LEDGER_CLIP = 2000  # a tool result recorded in the ledger is clipped shorter than the 8000-char read clip


def default_step_factory(cfg, control):
    """Resolved per turn, so an unset or changed model surfaces as that turn's error
    instead of a daemon that refuses to start. `control` reaches the HTTP stream itself
    (its `is_set()` covers both interrupt and a mid-stream steer cut)."""
    endpoint = resolve(cfg, cfg["model"])
    schemas = [spec.schema() for spec in tools.TOOLS.values()]  # the model must see its hands
    return lambda messages: openai_compat.stream(endpoint, messages, tools=schemas, cancel=control)


def _clip_for_ledger(result, limit=LEDGER_CLIP):
    if isinstance(result, str):
        return result if len(result) <= limit else result[:limit] + "...(truncated)"
    text = json.dumps(result, ensure_ascii=False)
    return result if len(text) <= limit else text[:limit] + "...(truncated)"


class Session:
    def __init__(self, sid):
        self.id = sid
        self.queue = asyncio.Queue()  # the lane: ("say", text) | ("resume", approval_id)
        self.conns = set()
        self.worker = None
        self.control: TurnControl | None = None  # of the running turn; None between turns
        self.turn = ""  # id of the running turn, valid while control is not None


class Daemon:
    def __init__(self, home, step_factory=None):
        self.home = Path(home)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.state.catch_up(self.ledger)  # a deleted or stale DB heals itself here
        self.step_factory = step_factory or default_step_factory
        self.system_prompt = PROMPT.read_text(encoding="utf-8")
        self.sessions = {}
        self.server = None

    # -- recording: ledger first, then the same event into the state. Loop thread only:
    #    the SQLite connection belongs to it, and turns run in worker threads.
    def record(self, type, **fields):
        event = self.ledger.append(type, **fields)
        self.state.apply(event)
        return event

    # -- socket ------------------------------------------------------------------------
    async def start(self):
        path = self.home / SOCKET
        if path.exists():
            if _answers(path):
                raise RuntimeError(f"a daemon is already serving {path}")
            path.unlink()  # left behind by a crash
        old = os.umask(0o177)  # the socket is born 0600: no window where another user can connect
        try:
            self.server = await asyncio.start_unix_server(self.handle, path=str(path), limit=LINE_LIMIT)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        return self.server

    async def serve(self):
        await self.start()
        async with self.server:
            await self.server.serve_forever()

    async def close(self):
        for s in self.sessions.values():
            if s.worker:
                s.worker.cancel()
            for w in list(s.conns):
                w.close()  # wait_closed() below waits for every connection: an attached client would hang shutdown
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        (self.home / SOCKET).unlink(missing_ok=True)
        self.state.close()

    async def handle(self, reader, writer):
        session = None
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
                if kind == "attach":
                    session = self.attach(writer, msg.get("session"), session)
                elif kind == "say":
                    text = msg.get("text")
                    if not isinstance(text, str) or not text.strip():
                        _write(writer, {"type": "error", "message": "say needs a non-empty \"text\""})
                        continue
                    session = session or self.attach(writer, None, None)
                    if session.control is not None:
                        self.steer(session, text)  # a turn is running: this is a mid-turn steer
                    else:
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
                    target = self._session(row["session"])
                    if session is not target:
                        if session:
                            session.conns.discard(writer)
                        target.conns.add(writer)
                        session = target
                    self.record("approval", session=row["session"], turn=row["turn"], origin="user",
                                id=approval_id, decision="approved" if kind == "approve" else "denied")
                    session.queue.put_nowait(("resume", approval_id))
                else:
                    _write(writer, {"type": "error", "message": f"unknown type: {kind!r}"})
        finally:
            if session:
                session.conns.discard(writer)
            writer.close()

    def _session(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid)
            s.worker = asyncio.create_task(self.lane(s))
        return s

    def attach(self, writer, sid, current):
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
        self.record("user", session=s.id, turn=s.turn, origin="user", trust="user", text=text, steer=True)
        s.control.steers.append(text)
        if not s.control.in_tool:  # the daemon does not cut a stream while a tool handler is running
            s.control.steer_cut.set()

    # -- turns -------------------------------------------------------------------------
    async def lane(self, s):
        while True:
            item_kind, payload = await s.queue.get()
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
                    s.control, s.turn = None, ""
            self.send(s, {"type": "turn.end", "turn": turn, "kind": kind})
            if leftover and kind != "gated":  # a gate took its leftovers into the pending messages
                s.queue.put_nowait(("continue", "\n".join(leftover)))

    async def turn(self, s, turn, text, trailing=None):
        """`text` is a new user message; `trailing` is steer text already in the ledger that no
        model call has seen yet (it arrived after the last drain of the previous turn)."""
        if text is not None:
            self.record("user", session=s.id, turn=turn, origin="user", trust="user", text=text)
        self.record("turn_start", session=s.id, turn=turn, origin="user")
        messages = [{"role": "system", "content": self.system_prompt}] + self.state.history(s.id)
        if trailing:
            messages.append({"role": "user", "content": trailing})
        return await self._run_turn(s, turn, messages, resume=False)

    async def resume_turn(self, s, turn, approval_id):
        row = self.state.approval(approval_id)
        self.record("turn_start", session=s.id, turn=turn, origin="user")
        approved = {row["call_id"]: (row["tool"], row["args"])} if row["decision"] == "approved" else {}
        denied = {row["call_id"]} if row["decision"] == "denied" else frozenset()
        return await self._run_turn(s, turn, row["messages"], resume=True, approved=approved, denied=denied)

    async def _run_turn(self, s, turn, messages, *, resume, approved=None, denied=frozenset()):
        self.send(s, {"type": "turn.start", "turn": turn})
        control = TurnControl()
        s.control, s.turn = control, turn

        aio = asyncio.get_running_loop()
        out_q = asyncio.Queue()

        def emit(kind, payload):  # called from the worker thread
            aio.call_soon_threadsafe(out_q.put_nowait, (kind, payload))

        cfg = config.load(self.home)
        broker = Broker(tools.TOOLS, "user", cfg["grants"], control=control, emit=emit,
                        approved=approved, denied=denied)

        def work(step, budget):
            try:
                return loop.run(step, messages, call_tool=broker.call, emit=emit,
                                budget=budget, control=control, resume=resume)
            finally:
                aio.call_soon_threadsafe(out_q.put_nowait, (None, None))

        try:
            running = asyncio.create_task(
                asyncio.to_thread(work, self.step_factory(cfg, control), cfg["max_steps"]))
            while True:
                kind, payload = await out_q.get()
                if kind is None:
                    break
                if kind == "delta":
                    self.send(s, {"type": "delta", "text": payload})
                elif kind == "tool_call":
                    recorded = {**payload, "args": _clip_for_ledger(payload["args"])}
                    self.record("tool_call", session=s.id, turn=turn, origin="user", **recorded)
                elif kind == "tool_result":
                    recorded = {**payload, "result": _clip_for_ledger(payload["result"])}
                    self.record("tool_result", session=s.id, turn=turn, origin="user", **recorded)
                    self.send(s, {"type": "tool", "name": payload["name"], "ok": payload["ok"]})
            out = await running
        except Gate as g:
            # The approval has its own id: the tool-call id is a string the model chose and
            # re-uses across turns, so it cannot be the key. Steers said after the last drain go
            # into the pending messages, or the resume would never see them.
            if g.said:
                self.record("assistant", session=s.id, turn=turn, origin="user", text=g.said, final=True)
            loop._drain(control, messages, envelope=False)
            approval_id = uuid.uuid4().hex[:12]
            self.record("approval_pending", session=s.id, turn=turn, origin="user", id=approval_id,
                        call_id=g.id, tool=g.name, args=g.arguments, messages=messages)
            self.send(s, {"type": "gate", "id": approval_id, "tool": g.name, "args": g.arguments})
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="gated")
            return "gated"
        except LLMError as e:
            # Out of credit, rate limited, offline, no model set: the assistant goes quiet,
            # the daemon does not die. Signals keep landing in the ledger.
            self.record("error", session=s.id, turn=turn, origin="user", status=e.status, message=str(e)[:300])
            self.send(s, {"type": "error", "message": str(e)[:300]})
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="error")
            return "error"

        if out.kind == "cancelled":
            self.record("interrupted", session=s.id, turn=turn, origin="user")
        if out.text:
            self.record("assistant", session=s.id, turn=turn, origin="user", text=out.text, final=True)
        self.record("turn_end", session=s.id, turn=turn, origin="user", kind=out.kind,
                    steps=out.steps, usage=out.usage)
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
