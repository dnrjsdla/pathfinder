"""The resident process: one unix socket, sessions, one lane per session.

Every fact is appended to the ledger first and projected into the state from that same
event, so deleting state.db loses nothing and live writes cannot drift from a rebuild.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import uuid
from pathlib import Path

from . import config, loop
from .ledger import Ledger
from .llm import LLMError, openai_compat, resolve
from .state import State

SOCKET = "pathfinder.sock"
PROMPT = Path(__file__).parent / "prompts" / "system.txt"
LINE_LIMIT = 1 << 20  # one NDJSON line; asyncio's 64 KiB default is too small for a pasted file


def default_step_factory(cfg, cancel):
    """Resolved per turn, so an unset or changed model surfaces as that turn's error
    instead of a daemon that refuses to start. `cancel` reaches the HTTP stream itself."""
    endpoint = resolve(cfg, cfg["model"])
    return lambda messages: openai_compat.stream(endpoint, messages, cancel=cancel)


def no_tools(name, args):
    return {"ok": False, "error": "no tools yet"}  # partition 2 puts the broker here


class Session:
    def __init__(self, sid):
        self.id = sid
        self.queue = asyncio.Queue()  # the lane: one turn at a time, a second `say` waits
        self.conns = set()
        self.worker = None
        self.cancel = None  # threading.Event of the running turn; interrupt and steer (partition 2) set it


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
                    session.queue.put_nowait(text)
                else:
                    _write(writer, {"type": "error", "message": f"unknown type: {kind!r}"})
        finally:
            if session:
                session.conns.discard(writer)
            writer.close()

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
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid)
            s.worker = asyncio.create_task(self.lane(s))
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

    # -- turns -------------------------------------------------------------------------
    async def lane(self, s):
        while True:
            text = await s.queue.get()
            turn = uuid.uuid4().hex[:12]
            try:
                kind = await self.turn(s, turn, text)
            except Exception as e:  # a bug in one turn must not end the session's lane
                kind = "error"
                self.send(s, {"type": "error", "message": f"internal error: {type(e).__name__}"})
                try:
                    self.record("error", session=s.id, turn=turn, message=f"{type(e).__name__}: {e}"[:300])
                    self.record("turn_end", session=s.id, turn=turn, origin="user", kind=kind)
                except Exception:
                    pass  # the ledger itself is failing (disk full?): a dead lane would turn every later `say` into silence
            self.send(s, {"type": "turn.end", "turn": turn, "kind": kind})

    async def turn(self, s, turn, text):
        self.record("user", session=s.id, turn=turn, origin="user", trust="user", text=text)
        self.record("turn_start", session=s.id, turn=turn, origin="user")
        self.send(s, {"type": "turn.start", "turn": turn})
        messages = [{"role": "system", "content": self.system_prompt}] + self.state.history(s.id)

        # The turn blocks, so it runs in a thread and hands its output back through one queue.
        # The queue - not event-loop scheduling - is what orders the stream: the end marker is
        # put by the same thread after its last delta, so no delta can arrive after turn.end.
        aio = asyncio.get_running_loop()
        out_q = asyncio.Queue()

        def emit(kind, payload):  # called from the worker thread
            msg = {"type": "delta", "text": payload} if kind == "delta" else {"type": "tool", **payload}
            aio.call_soon_threadsafe(out_q.put_nowait, msg)

        def work(step, budget):
            try:
                return loop.run(step, messages, call_tool=no_tools, emit=emit, budget=budget)
            finally:
                aio.call_soon_threadsafe(out_q.put_nowait, None)

        try:
            cfg = config.load(self.home)
            s.cancel = threading.Event()
            running = asyncio.create_task(asyncio.to_thread(work, self.step_factory(cfg, s.cancel), cfg["max_steps"]))
            while (msg := await out_q.get()) is not None:
                self.send(s, msg)
            out = await running
        except LLMError as e:
            # Out of credit, rate limited, offline, no model set: the assistant goes quiet,
            # the daemon does not die. Signals keep landing in the ledger.
            self.record("error", session=s.id, turn=turn, status=e.status, message=str(e)[:300])
            self.send(s, {"type": "error", "message": str(e)[:300]})
            self.record("turn_end", session=s.id, turn=turn, origin="user", kind="error")
            return "error"
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
