"""pathfinder daemon | reindex | say TEXT | config set KEY VALUE |
approvals | approve ID | deny ID | interrupt |
listen | signal SENSE JSON | react up|down [ID] | rules | tools |
jobs | job stop ID | job steer ID TEXT |
memory | memory run | memory undo [N] |
install NAME | uninstall ID | search TEXT | secret ID NAME |
init | service start|stop|restart|status | tui"""
from __future__ import annotations

import asyncio
import getpass
import json
import shlex
import socket
import sys
import time
from pathlib import Path

from . import catalog, config, keychain, memory, mirror, tui
from . import init as init_mod
from . import installer as installer_mod
from .clock import format_local
from .daemon import SOCKET, Daemon, is_serving
from .ledger import Ledger
from .mcp import servers as mcp_servers
from .mcp.protocol import qualify
from .state import State
from .tools import visible

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
        elif msg["type"] == "retract":  # D51: the draft above is withdrawn, the re-run follows
            print(flush=True)  # the withdrawn draft and the re-run stay on lines of their own
            print(tui.STRINGS["retract"].format(what=msg["what"]), file=sys.stderr)
        elif msg["type"] == "gate":
            args = json.dumps(msg["args"], ensure_ascii=False)  # the same rendering as the screen's and `approvals`'
            print("\n" + visible(f"pathfinder: waiting for approval on {msg['tool']} {args} "
                                 f"(run `pathfinder approve {msg['id']}` or `pathfinder deny {msg['id']}`)"), file=sys.stderr)
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


def _clip60(text):
    return text if len(text) <= 60 else text[:60] + "..."


def approvals(home):
    """List pending approvals directly from state.db - no daemon protocol needed to read them."""
    state = State(home / "state.db")
    try:
        for row in state.pending():
            if row["tool"] == "edit_memory":
                a = row["args"]
                old, new = (json.dumps(_clip60(a[k]), ensure_ascii=False) for k in ("old", "new"))  # as the args rows
                print(visible(f"{row['id']}  edit_memory  {a['target']} {a['op']}: {old} -> {new}"))
                continue
            if row["tool"] == "accept_pins":
                a = row["args"]
                held = a.get("held") or []
                print(f"{row['id']}  accept_pins  {a['server']}: {len(held)} held — {held[0] if held else ''}")
                continue
            if row["tool"] == "install_server":
                print(installer_mod.render_row(row))
                continue
            job = f"  job: {row['job']}" if row.get("job") else ""
            print(visible(f"{row['id']}  {row['tool']}  {json.dumps(row['args'], ensure_ascii=False)}{job}"))
    finally:
        state.close()
    return 0


def mcp_tools_list(home):
    """`pathfinder tools`: reads config + state directly, like `approvals` - no daemon needed
    to see the tool surface."""
    cfg = config.load(home)
    state = State(home / "state.db")
    try:
        pins, grants = state.pins(), cfg["grants"]
        held_rows = [r for r in state.pending() if r["tool"] == "accept_pins"]
        held_names = {qualify(row["args"]["server"], t["name"])
                      for row in held_rows for t in row["args"]["tools"]}
        for server_cfg in mcp_servers.load(cfg):
            sid = server_cfg.server_id
            if server_cfg.disabled:
                print(f"{sid}  {server_cfg.transport_kind}  disabled: {server_cfg.disabled}")
                continue
            tool_names = sorted(pins.get(sid, {}))
            print(f"{sid}  {server_cfg.transport_kind}  {len(tool_names)} tools")
            for tool_name in tool_names:
                name = qualify(sid, tool_name)
                label = "held" if name in held_names else grants.get(name, "ask")
                print(f"  {visible(name)}  {label}")
        if held_rows:
            print("held:")
            for row in held_rows:
                for line in row["args"]["held"]:
                    print(f"  {line}")
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
                text = tui.render(msg)  # one renderer for every server message (partition 7)
                if text is None:
                    continue
                if msg["type"] == "error":
                    print(text, file=sys.stderr)
                elif msg["type"] in ("delta", "turn.end"):
                    print(text, end="", flush=True)  # a pipe is block-buffered
                else:
                    print("\n" + text, flush=True)  # on its own line, even after a stream
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


def install_cmd(home, name):
    """`pathfinder install NAME`: check + propose over the socket, print the proposal's row."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        f = sock.makefile("rwb")
        f.write((json.dumps({"type": "install", "name": name}) + "\n").encode())
        f.flush()
        line = f.readline()
        if not line:
            print("pathfinder: no reply", file=sys.stderr)
            return 1
        msg = json.loads(line)
        if msg["type"] == "error":
            print(f"pathfinder: {msg['message']}", file=sys.stderr)
            return 1
        print(msg["line"])
    return 0


def uninstall_cmd(home, id):
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        f = sock.makefile("rwb")
        f.write((json.dumps({"type": "uninstall", "id": id}) + "\n").encode())
        f.flush()
        line = f.readline()
        if not line:
            print("pathfinder: no reply", file=sys.stderr)
            return 1
        msg = json.loads(line)
        if msg["type"] == "error":
            print(f"pathfinder: {msg['message']}", file=sys.stderr)
            return 1
        print(f"uninstalled {msg['id']}")
    return 0


def search_cmd(home, text):
    """`pathfinder search TEXT`: the registry copy on disk - no daemon needed, and it never syncs or creates it."""
    if not (Path(home) / "registry.db").exists():
        senses = config.load(home).get("senses") or {}
        print("no registry copy yet: the daemon fills it while the catalog sense is on"
              + (" (it is on: the copy starts with the daemon's next poll)" if "catalog" in senses else
                 f" - turn it on, keeping the others, with:\npathfinder config set senses "
                 f"{shlex.quote(json.dumps({**senses, 'catalog': {}}))}"))
        return 0
    items, note = mirror.search(home, text)
    for item in items:
        print(f"{catalog.listing(item)}  {catalog.oneline(item['server'].get('version', ''), 40)}")
    if note or not items:
        print(note or "no server in the registry copy matches; search English words (gmail, brave search, weather)")
    return 0


def secret_cmd(id, name):
    """`pathfinder secret ID NAME`: the value read hidden and put straight into the Keychain -
    no socket, no daemon, no ledger."""
    if not keychain.valid_target(id, name):
        print("pathfinder: secret ID NAME takes a server id and an env variable name", file=sys.stderr)
        return 2
    try:
        value = getpass.getpass(f"{name} for {id} (hidden; stored in the Keychain): ")
    except (EOFError, KeyboardInterrupt):
        print("\nnothing stored", file=sys.stderr)
        return 1
    try:
        keychain.store(f"pathfinder-mcp-{id}", name, value)
    except Exception as e:  # store's messages never carry the value
        print(f"pathfinder: {e}", file=sys.stderr)
        return 1
    print(f"{name} stored for {id}; an installed server picks it up at the next `pathfinder service restart`")
    return 0


def rules_path(home):
    print(str(Path(home) / "RULES.md"))
    return 0


def jobs_list(home):
    """List every job, newest first - reads state.db directly, like `approvals`."""
    state = State(home / "state.db")
    try:
        for row in state.jobs():
            started = format_local(row["started_ts"])
            print(f"{row['id']}  {row['state']}  {started}  {row['goal']}")
    finally:
        state.close()
    return 0


def job_stop(home, id):
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        sock.sendall((json.dumps({"type": "job.stop", "id": id}) + "\n").encode())
    return 0


def job_steer(home, id, text):
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        sock.sendall((json.dumps({"type": "job.steer", "id": id, "text": text}) + "\n").encode())
    return 0


def memory_status(home):
    """`pathfinder memory`: cursor, passes/lines-read/applied for today and the two days before,
    pending proposals, judgments per day (seven days, today first), and the latest session's
    summary - reads state.db directly, like `jobs_list`."""
    state = State(home / "state.db")
    try:
        stats = state.memory_stats(time.time())
        print(f"cursor {state.memory_cursor}")
        labels = ("today", "1 day ago", "2 days ago")
        for label, day in zip(labels, stats["days"]):
            print(f"{label}: passes {day['passes']}, read {day['read']}, applied {day['applied']}")
        print(f"pending proposals {stats['pending']}")
        print(f"judgments/day (today first): {' '.join(str(n) for n in stats['judgments'])}")
        sid = state.latest_session(config.load(home)["session_resume_s"])
        summary = state.summary(sid) if sid else None
        if summary:
            print(f"latest session summary upto #{summary['upto']}")
    finally:
        state.close()
    return 0


def memory_run(home):
    """`pathfinder memory run`: force a pass over the socket, print its result line."""
    sock = _connect(home)
    if sock is None:
        return 1
    with sock:
        f = sock.makefile("rwb")
        f.write((json.dumps({"type": "memory.run"}) + "\n").encode())
        f.flush()
        line = f.readline()
        if not line:
            print("pathfinder: no reply", file=sys.stderr)
            return 1
        msg = json.loads(line)
        if msg["type"] == "error":
            print(f"pathfinder: {msg['message']}", file=sys.stderr)
            return 1
        print(msg["result"])
    return 0


def memory_undo(home, n=1):
    """`pathfinder memory undo [N]`: refuses while a daemon is serving (like `reindex`); builds
    its own Ledger/State/Manager, the same pattern `reindex` uses for a standalone run."""
    if is_serving(home):
        print("pathfinder: stop the daemon first (or just restart it - it rebuilds what is missing)", file=sys.stderr)
        return 1
    ledger, state = Ledger(home), State(home / "state.db")

    def record(type, **fields):
        event = ledger.append(type, **fields)
        state.apply(event)
        return event

    mgr = memory.Manager(home, record, lambda signal: None, None, None, [], is_busy=lambda: False)
    try:
        for line in mgr.undo(n):
            print(line)
    finally:
        mgr.close()
        state.close()
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    home = config.home()
    if args == ["init"]:
        return init_mod.run(home)
    if len(args) == 2 and args[0] == "service" and args[1] in ("start", "stop", "restart", "status"):
        return init_mod.service(home, args[1])
    if args == ["daemon"]:
        config.make_private(home)  # every upgrade, and launchd's daemon.log
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
        memory.reindex_pages(home, config.load(home))
        state = State(home / "state.db")
        pages = len(state.pages())
        state.close()
        print(f"rebuilt state up to event {n}, {pages} wiki page(s) indexed")
        return 0
    if len(args) == 2 and args[0] == "say":
        return say(home, args[1])
    if args == ["approvals"]:
        return approvals(home)
    if len(args) == 2 and args[0] in ("approve", "deny"):
        return decide(home, args[0], args[1])
    if args == ["interrupt"]:
        return interrupt(home)
    if args == ["tui"]:
        return tui.main(home)
    if args == ["listen"]:
        return listen(home)
    if len(args) == 3 and args[0] == "signal":
        return signal(home, args[1], args[2])
    if len(args) in (2, 3) and args[0] == "react" and args[1] in ("up", "down"):
        return react(home, args[1], args[2] if len(args) == 3 else None)
    if args == ["rules"]:
        return rules_path(home)
    if args == ["tools"]:
        return mcp_tools_list(home)
    if len(args) == 2 and args[0] == "install":
        return install_cmd(home, args[1])
    if len(args) == 2 and args[0] == "uninstall":
        return uninstall_cmd(home, args[1])
    if len(args) >= 2 and args[0] == "search":
        return search_cmd(home, " ".join(args[1:]))
    if len(args) == 3 and args[0] == "secret":
        return secret_cmd(args[1], args[2])
    if args == ["jobs"]:
        return jobs_list(home)
    if len(args) == 3 and args[0] == "job" and args[1] == "stop":
        return job_stop(home, args[2])
    if len(args) == 4 and args[0] == "job" and args[1] == "steer":
        return job_steer(home, args[2], args[3])
    if args == ["memory"]:
        return memory_status(home)
    if args == ["memory", "run"]:
        return memory_run(home)
    if len(args) in (2, 3) and args[:2] == ["memory", "undo"]:
        if len(args) == 3 and not args[2].isdigit():
            print(USAGE, file=sys.stderr)
            return 2
        return memory_undo(home, int(args[2]) if len(args) == 3 else 1)
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
