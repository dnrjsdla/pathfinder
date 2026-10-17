#!/usr/bin/env python3
"""p1_runs.py -- the partition-1 live check (docs/plans/v2-partition-1.md, "The live check"), steps 1-6 in one run.

Live:    python3 bench/v2/p1_runs.py --pf /tmp/pf1-venv/bin/pathfinder --root-model P:M --fallback-model P:M
Dry run: python3 bench/v2/p1_runs.py --dry-run   (p1_checks' step checks on a synthetic ledger, each step also
         shown to FAIL on a broken copy; no daemon, no model, no Keychain)

The live run recreates /tmp/pf1 (0700), writes the config through `pathfinder config set` (the root ref's provider
gets api anthropic at effort xhigh, the fallback ref's provider the local Ollama URL), starts and stops the daemon by
its pid only, and checks the ledger (/tmp/pf1/events/*.jsonl) and the root's row in state.db with p1_checks. The
root's key comes from the daemon's own Keychain lookup. /tmp/pf1 is removed when every step passes, kept otherwise.
"""
import argparse, json, os, shutil, signal, socket, subprocess, sys, time
from pathlib import Path

import fake_notion  # next to this file
from p1_checks import CHECKS, dry_run, events, uses

HOME = Path("/tmp/pf1")
OLLAMA_URL = "http://localhost:11434/v1"
TITLE = "휴가 계획"
SAY = {1: f"Open {HOME}/workspace/pages.json and give me the exact id of the page titled '{TITLE}'.",
       2: f"Write that id, and nothing else, into {HOME}/workspace/answer.txt.",
       4: "Run the shell command: sleep 30", "4b": "Did the last command finish?", 5: "Say hello in five words."}


def report(rows, notes):
    """The per-step table: FAIL if a check failed, else UNTESTED if one could not be shown, else PASS. True only
    when every check passed."""
    print(f"{'step':<10}{'result':<10}checks")
    for step, checks in rows:
        failed, untested = [label for label, ok in checks if ok is False], [label for label, ok in checks if ok is None]
        status = "FAIL" if failed else "UNTESTED" if untested else "PASS"
        print(f"{step!s:<10}{status:<10}{'; '.join(failed + untested) or f'all {len(checks)} passed'}")
    print("\n".join(notes))
    return bool(rows) and all(ok is True for _, checks in rows for _, ok in checks)


def serving(home):
    """A connect, not the socket file: after a kill -9 the dead daemon's file stays until the next one binds."""
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(str(home / "pathfinder.sock"))
            return True
        except OSError:
            return False


def live(pf, root, fallback):
    env = {**os.environ, "PATHFINDER_HOME": str(HOME)}
    run = lambda *args: subprocess.run([pf, *args], env=env, capture_output=True, text=True, timeout=600)
    last = lambda: max((e["seq"] for e in events(HOME)), default=0)

    def out(*args):
        p = run(*args)
        return p.stdout + p.stderr

    shutil.rmtree(HOME, ignore_errors=True)
    for d in (HOME, HOME / "check", HOME / "workspace"):
        d.mkdir(mode=0o700)
    world = fake_notion.World()
    (HOME / "workspace" / "pages.json").write_text(json.dumps({"results": world.pages}, ensure_ascii=False))
    want = world.by_title[TITLE]["id"]
    (HOME / "check" / "want").write_text(want)
    rp, fp = root.split(":", 1)[0], fallback.split(":", 1)[0]
    providers = {rp: {"api": "anthropic", "base_url": "https://api.anthropic.com", "effort": "xhigh"}, fp: {"base_url": OLLAMA_URL}}
    for key, value in (("providers", providers), ("models", {"root": [root, fallback], "summarize": [root]})):
        if (p := run("config", "set", key, json.dumps(value))).returncode:
            sys.exit(p.stdout + p.stderr)
    log, daemon = open(HOME / "check" / "log", "ab"), None

    def start():
        nonlocal daemon
        daemon = subprocess.Popen([pf, "daemon"], env=env, stdout=log, stderr=subprocess.STDOUT)
        (HOME / "check" / "pid").write_text(str(daemon.pid))
        for _ in range(50):
            if serving(HOME):
                return
            time.sleep(0.2)
        raise RuntimeError(f"the daemon did not start in 10 s; see {HOME}/check/log")

    def stop(sig=signal.SIGTERM):
        """This pid only, never by name; a daemon still up 30 s later is killed. Returns the check for the step."""
        daemon.send_signal(sig)
        try:
            daemon.wait(30)
            return (f"the daemon exited on {sig.name}", True)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
            return (f"the daemon was still up 30 s after {sig.name}: forced kill", False)

    c, rows = {"home": HOME, "want": want, "root": root, "fallback": fallback, "out": {}, "notes": []}, []
    try:
        start()
        mark, c["out"]["say1"] = last(), out("say", SAY[1])
        rows.append((1, CHECKS[1](events(HOME, mark), c)))
        mark, c["out"]["say2"], c["out"]["approvals2"] = last(), out("say", SAY[2]), out("approvals")
        rows.append((2, CHECKS[2](events(HOME, mark), c)))
        stopped = stop()
        for path in HOME.glob("state.db*"):
            path.unlink()
        mark, c["before"] = last(), events(HOME)
        start()
        c["out"]["approvals3"], c["out"]["approve3"] = out("approvals"), out("approve", str(c["gate"]))
        rows.append((3, CHECKS[3](events(HOME, mark), c) + [stopped]))
        mark = last()
        say = subprocess.Popen([pf, "say", SAY[4]], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(600):  # until the call is recorded (the contract's grep), at most 120 s
            if uses(events(HOME, mark), "run_command") or say.poll() is not None:
                break
            time.sleep(0.2)
        killed = stop(signal.SIGKILL)
        say.wait(30)
        start()
        c["out"]["say4"] = out("say", SAY["4b"])
        rows.append((4, CHECKS[4](events(HOME, mark), c) + [killed]))
        run("config", "set", "providers", json.dumps({**providers, rp: {"api": "anthropic", "base_url": "http://127.0.0.1:9"}}))
        mark, c["out"]["say5"] = last(), out("say", SAY[5])
        rows.append((5, CHECKS[5](events(HOME, mark), c)))
        rows.append((6, [stop()]))
    finally:
        if daemon is not None and daemon.poll() is None:
            rows.append(("cleanup", [stop()]))
        passed = report(rows, c["notes"])
    if passed:
        shutil.rmtree(HOME)
    print(f"{'removed' if passed else 'kept for reading:'} {HOME}")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="the partition-1 live check, steps 1-6")
    ap.add_argument("--dry-run", action="store_true", help="the checks on a synthetic ledger; nothing is started")
    ap.add_argument("--pf", default="/tmp/pf1-venv/bin/pathfinder", help="the pathfinder CLI installed from this tree")
    ap.add_argument("--root-model", help="provider:model; that provider is configured as api anthropic")
    ap.add_argument("--fallback-model", help="provider:model; that provider is configured as the local Ollama")
    a = ap.parse_args()
    if a.dry_run:
        passed = report(dry_run(), [])
        print("dry run:", "PASS" if passed else "FAIL")
        return 0 if passed else 1
    if ":" not in (a.root_model or "") or ":" not in (a.fallback_model or ""):
        ap.error("--root-model and --fallback-model are required, each provider:model")
    return live(a.pf, a.root_model, a.fallback_model)


if __name__ == "__main__":
    sys.exit(main())
