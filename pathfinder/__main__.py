"""pathfinder daemon | reindex | say TEXT | config set KEY VALUE"""
from __future__ import annotations

import asyncio
import json
import socket
import sys

from . import config
from .daemon import SOCKET, Daemon, is_serving
from .ledger import Ledger
from .state import State

USAGE = __doc__


def say(home, text):
    """The thinnest possible client: attach, say, print deltas until the turn ends."""
    path = home / SOCKET
    with socket.socket(socket.AF_UNIX) as sock:
        try:
            sock.connect(str(path))
        except OSError:
            print("pathfinder: the daemon is not running (start it with: pathfinder daemon)", file=sys.stderr)
            return 1
        f = sock.makefile("rwb")
        for msg in ({"type": "attach"}, {"type": "say", "text": text}):
            f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        f.flush()
        failed = False
        for line in f:
            msg = json.loads(line)
            if msg["type"] == "delta":
                print(msg["text"], end="", flush=True)
            elif msg["type"] == "error":
                print(f"\npathfinder: {msg['message']}", file=sys.stderr)
                failed = True
            elif msg["type"] == "turn.end":
                print()
                return 1 if failed or msg["kind"] == "error" else 0
    return 1


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
