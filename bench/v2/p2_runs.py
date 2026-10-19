#!/usr/bin/env python3
"""p2_runs.py -- the partition-2 live check (docs/plans/v2-partition-2.md, "The live check"), steps 0-5 in one run.

Live:    python3 bench/v2/p2_runs.py --root-model anthropic:M --fallback-model ollama:M --embed-model ollama:M
Dry run: python3 bench/v2/p2_runs.py --dry-run   (p2_checks' checks find, gate, fallback, ollama on a synthetic
         ledger, each also shown to FAIL on a broken copy; nothing started, no model called)

The live run recreates /tmp/pf2 (0700) and a fresh /tmp/pf2-venv with this tree installed, configures the providers
(anthropic at effort xhigh, ollama local), the models, the embed model and the fake Notion server (fake_notion_mcp.py
over /tmp/pf2/check/world.json) through `pathfinder config set`, starts and stops the daemon by its pid only (never by
name), and checks the ledger. Model refs come from the command line only; a root `ollama:M` makes a mechanics-only run
with no Anthropic request. Both directories are removed when every check passes, kept otherwise.
"""
import argparse, json, os, shutil, signal, subprocess, sys, time
from pathlib import Path

from p1_checks import events, find
from p1_runs import OLLAMA_URL, report, serving
from p2_checks import CHECKS, SERVER, TITLE, dry_run

HOME, VENV, HERE = Path("/tmp/pf2"), Path("/tmp/pf2-venv"), Path(__file__).resolve().parent
SAY = {1: f"In Notion, when was the page titled '{TITLE}' last edited?", 2: "Archive that page.",
       3: "In Notion, search for pages about 휴가.", 4: "In Notion, create a page titled 'P2 check' with the property Status = Done."}


def live(root, fallback, embed):
    env = {**os.environ, "PATHFINDER_HOME": str(HOME)}
    pf = str(VENV / "bin" / "pathfinder")
    run = lambda *a: subprocess.run([pf, *a], env=env, capture_output=True, text=True, timeout=600)
    out = lambda *a: (lambda p: p.stdout + p.stderr)(run(*a))
    last = lambda: max((e["seq"] for e in events(HOME)), default=0)
    world = lambda: json.loads((HOME / "check" / "world.json").read_text())
    for d in (HOME, VENV):
        shutil.rmtree(d, ignore_errors=True)
    for d in (HOME, HOME / "check"):
        d.mkdir(mode=0o700)
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin" / "pip"), "install", "-q", "-e", str(HERE.parents[1])], check=True)
    server = {"transport": "stdio", "command": [sys.executable, str(HERE / "fake_notion_mcp.py"), str(HOME / "check" / "world.json")]}

    def setting(key, value):
        """`config set`, or exit (a running daemon is still stopped by the `finally` below)."""
        if (p := run("config", "set", key, json.dumps(value))).returncode:
            sys.exit(p.stdout + p.stderr)

    for key, value in (("providers", {"anthropic": {"api": "anthropic", "base_url": "https://api.anthropic.com", "effort": "xhigh"},
                                      "ollama": {"base_url": OLLAMA_URL}}),
                       ("models", {"root": [root]}), ("embed_model", embed), ("mcp_servers.fake-notion", server)):
        setting(key, value)
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

    def stop():
        """This pid only, never by name; a daemon still up 30 s after SIGTERM is killed."""
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(30)
            return ("the daemon exited on SIGTERM", True)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
            return ("the daemon was still up 30 s after SIGTERM: forced kill", False)

    def step(n, *before):
        c["mark"] = last()
        c["out"][f"say{n}"] = out("say", SAY[n])
        if n in (2, 4):
            c["out"][f"approvals{n}"] = out("approvals")
            if g := find(events(HOME, c["mark"]), "gate", tool=SERVER + "pages"):
                c["out"][f"approve{n}"] = out("approve", str(g["seq"]))
        c["world"] = world()
        rows.append((n, CHECKS[n](events(HOME), c)))

    c, rows = {"out": {}, "notes": [], "draft": {}}, []
    try:
        start()
        for _ in range(150):  # Attach drafts the map at the server's first load, in the background after the bind
            if any(e["type"] == "config_set" and e.get("key") == "tools" for e in events(HOME)):
                break
            time.sleep(0.2)
        try:
            c["draft"] = json.loads(out("config", "get", "tools.fake-notion"))
        except ValueError:
            pass
        step(1)
        step(2)
        setting("embed_model", "ollama:no-such-model")
        step(3)
        setting("models", {"root": [fallback]})  # a failure here would leave step 4 on the root model
        c["pages0"] = len(world()["pages"])
        step(4)
        rows.append((5, [stop()]))
    finally:
        if daemon is not None and daemon.poll() is None:
            rows.append(("cleanup", [stop()]))
        passed = report(rows, c["notes"])
    if passed:
        shutil.rmtree(HOME)
        shutil.rmtree(VENV)
    print(f"{'removed' if passed else 'kept for reading:'} {HOME} {VENV}")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="the partition-2 live check, steps 0-5")
    ap.add_argument("--dry-run", action="store_true", help="the checks on a synthetic ledger; nothing is started")
    ap.add_argument("--root-model", help="anthropic:MODEL (ollama:MODEL for a mechanics-only run), the root for steps 1-3")
    ap.add_argument("--fallback-model", help="ollama:MODEL, the root for step 4")
    ap.add_argument("--embed-model", help="ollama:MODEL, the tool-search embeddings")
    a = ap.parse_args()
    if a.dry_run:
        passed = report(dry_run(), [])
        print("dry run:", "PASS" if passed else "FAIL")
        return 0 if passed else 1
    refs = (a.root_model or "", a.fallback_model or "", a.embed_model or "")
    if not refs[0].startswith(("anthropic:", "ollama:")) or not all(r.startswith("ollama:") for r in refs[1:]):
        ap.error("--root-model anthropic:M|ollama:M, --fallback-model ollama:M and --embed-model ollama:M are required")
    return live(*refs)


if __name__ == "__main__":
    sys.exit(main())
