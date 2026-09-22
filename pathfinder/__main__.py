"""pathfinder daemon | reindex | say TEXT | config set KEY VALUE |
approvals | approve ID | deny ID | interrupt |
listen | signal SENSE JSON | react up|down [ID] | rules"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

from . import config
from .daemon import SOCKET, Daemon, is_serving
from .ledger import Ledger
from .state import State

USAGE = __doc__


def _connect(home):
    path = home / SOCKET
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        print("pathfinder: the daemon is not running (start it with: pathfinder daemon)", file=sys.stderr)
        return None
    return sock


def _stream(sock, messages):
    """Send `messages`, print deltas (and gate/error notices) until turn.end. Returns the exit code."""
    f = sock.makefile("rwb")
    for msg in messages:
        f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
    f.flush()
    failed = started = False
    for line in f:
        msg = json.loads(line)
        if msg["type"] == "turn.start":
            started = True
        elif msg["type"] == "delta":
            print(msg["text"], end="", flush=True)
        elif msg["type"] == "error":
            print(f"\npathfinder: {msg['message']}", file=sys.stderr)
            if not started:
                return 1  # refused before any turn began (unknown approval, nothing to interrupt): no turn.end will come
            failed = True
        elif msg["type"] == "gate":
            print(f"\npathfinder: waiting for approval on {msg['tool']} {msg['args']} "
                  f"(run `pathfinder approve {msg['id']}` or `pathfinder deny {msg['id']}`)", file=sys.stderr)
        elif msg["type"] == "turn.end":
            print()
            return 1 if failed or msg["kind"] == "error" else 0
    return 1


def say(home, text):
    """The thinnest possible client: attach, say, print deltas until the turn ends."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        return _stream(sock, [{"type": "attach"}, {"type": "say", "text": text}])


def approvals(home):
    """List pending approvals directly from state.db - no daemon protocol needed to read them."""
    state = State(home / "state.db")
    try:
        for row in state.pending():
            print(f"{row['id']}  {row['tool']}  {json.dumps(row['args'], ensure_ascii=False)}")
    finally:
        state.close()
    return 0


def decide(home, kind, approval_id):
    """approve/deny: send the decision, print the resumed turn's answer until it ends."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        return _stream(sock, [{"type": kind, "id": approval_id}])


def interrupt(home):
    """Attach to the latest session and stop its running turn."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        return _stream(sock, [{"type": "attach"}, {"type": "interrupt"}])


def listen(home):
    """Attach and print notices and deltas until Ctrl-C - the live check's window."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        f = sock.makefile("rwb")
        f.write((json.dumps({"type": "attach"}) + "\n").encode())
        f.flush()
        try:
            for line in f:
                msg = json.loads(line)
                if msg["type"] == "delta":
                    print(msg["text"], end="", flush=True)
                elif msg["type"] == "notice":
                    print(f"\n[signal] {msg['text']}  ({msg['why']})", flush=True)  # a pipe is block-buffered
                elif msg["type"] == "turn.end":
                    print(flush=True)
                elif msg["type"] == "error":
                    print(f"\npathfinder: {msg['message']}", file=sys.stderr)
        except KeyboardInterrupt:
            print()
    return 0


def signal(home, sense, fields_json):
    """The hook client: connect, send, exit without waiting - a hook must never slow the shell
    down, so a daemon that is not running (or bad JSON) is silent, not an error."""
    path = home / SOCKET
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(str(path))
        fields = json.loads(fields_json)
        with sock:
            sock.sendall((json.dumps({"type": "signal", "sense": sense, "fields": fields}) + "\n").encode())
    except (OSError, ValueError):
        pass
    return 0


def react(home, vote, delivery_id=None):
    """React to a delivery - defaults to the latest one (the daemon's own choice)."""
    sock = _connect(home)
    if sock is None:
        return 1
    msg = {"type": "react", "vote": vote}
    if delivery_id:
        msg["id"] = delivery_id
    with sock:
        f = sock.makefile("rwb")
        f.write((json.dumps(msg) + "\n").encode())
        f.flush()
        sock.settimeout(1)
        try:
            line = f.readline()
        except OSError:
            line = b""
        if line:
            resp = json.loads(line)
            if resp.get("type") == "error":
                print(f"pathfinder: {resp['message']}", file=sys.stderr)
                return 1
    return 0


def rules_path(home):
    print(str(Path(home) / "RULES.md"))
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    home = config.home()
    if args == ["daemon"]:
        try:
            asyncio.run(Daemon(home).serve())
        except KeyboardInterrupt:
            pass
        return 0
    if args == ["reindex"]:
        if is_serving(home):
            # Dropping tables under a live daemon double-counts and races its projection.
            # Not needed anyway: a restarted daemon catches up from the ledger by itself.
            print("pathfinder: stop the daemon first (or just restart it - it rebuilds what is missing)", file=sys.stderr)
            return 1
        state = State(home / "state.db")
        n = state.rebuild(Ledger(home))
        state.close()
        print(f"rebuilt state up to event {n}")
        return 0
    if len(args) == 2 and args[0] == "say":
        return say(home, args[1])
    if args == ["approvals"]:
        return approvals(home)
    if len(args) == 2 and args[0] in ("approve", "deny"):
        return decide(home, args[0], args[1])
    if args == ["interrupt"]:
        return interrupt(home)
    if args == ["listen"]:
        return listen(home)
    if len(args) == 3 and args[0] == "signal":
        return signal(home, args[1], args[2])
    if len(args) in (2, 3) and args[0] == "react" and args[1] in ("up", "down"):
        return react(home, args[1], args[2] if len(args) == 3 else None)
    if args == ["rules"]:
        return rules_path(home)
    if len(args) == 4 and args[:2] == ["config", "set"]:
        try:
            value = json.loads(args[3])
        except ValueError:
            value = args[3]  # a bare word is a string: `config set model provider:name`
        config.set(home, args[2], value, ledger=Ledger(home))
        print(f"{args[2]} set")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
