"""m5 - one approval installs in five minutes (partition 6 entrance measurement, zero model credits).

In a temp HOME (a hand-written ~/.claude.json with two unrelated keys and one unrelated server)
and a temp PATHFINDER_HOME, for npm servers picked from the official registry at run time
(pinned version, no secret or required env - the reference servers are not in the registry,
see HISTORY): `pathfinder install <name>` -> the proposal row -> `pathfinder approve` (the bench
answers, no model runs) -> timing of every install_step from the ledger -> `pathfinder uninstall`
-> both files deep-equal to their pre-install content. Attempts continue until three installs
succeed (an upstream package that fails its own trial is the product's `install_failed`, recorded
and skipped), at most MAX_ATTEMPTS. Pass: 3 installed, each <= 300 s, 3 rolled back, every
pre-existing key preserved.

    python3 bench/m5_install.py [--attempts 8] [--out docs/measurements/m5-<date>.json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"
MAX_ATTEMPTS = 8
LIMIT_S = 300
PAGES = 8

UNRELATED_CLAUDE_JSON = {
    "numStartups": 3,
    "theme": "dark",
    "mcpServers": {"unrelated": {"command": "echo", "args": ["hi"], "env": {}}},
}


def candidates(pages: int = PAGES) -> list:
    """npm entries with a concrete version and nothing the user must supply, registry order."""
    out, cursor = [], None
    for _ in range(pages):
        q = {"version": "latest", "limit": 100}
        if cursor:
            q["cursor"] = cursor
        with urllib.request.urlopen(REGISTRY + "?" + urllib.parse.urlencode(q), timeout=25) as r:
            d = json.load(r)
        for s in d.get("servers", []):
            srv = s["server"]
            for p in srv.get("packages", []):
                if p.get("registryType") != "npm" or p.get("version") in (None, "", "latest"):
                    continue
                if any(e.get("isSecret") or e.get("isRequired") for e in p.get("environmentVariables", [])):
                    continue
                if any(a.get("isRequired") for a in p.get("packageArguments", []) + p.get("runtimeArguments", [])):
                    continue
                out.append(srv["name"])
                break
        cursor = (d.get("metadata") or {}).get("nextCursor")
        if not cursor:
            break
    return out


class Env:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m5-"))
        self.home = self.tmp / "home"
        self.pf = self.tmp / "pf"  # short: AF_UNIX paths are capped at 104 bytes
        self.home.mkdir()
        self.pf.mkdir()
        (self.home / ".claude.json").write_text(json.dumps(UNRELATED_CLAUDE_JSON, indent=2) + "\n")
        self.env = {**os.environ, "HOME": str(self.home), "PATHFINDER_HOME": str(self.pf)}
        self.daemon = None

    def cli(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pathfinder", *args], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def start(self):
        self.cli("config", "set", "senses", "{}")
        self.log = open(self.tmp / "daemon.log", "w")  # a pipe nobody reads would deadlock at 64 KB (review)
        self.daemon = subprocess.Popen([sys.executable, "-m", "pathfinder", "daemon"], cwd=ROOT, env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=self.log, text=True, start_new_session=True)
        for _ in range(50):
            if any(self.pf.glob("*.sock")):
                return
            time.sleep(0.2)
        raise RuntimeError("daemon did not start: " + (self.tmp / "daemon.log").read_text()[-500:])

    def stop(self):
        if self.daemon:
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
            try:  # the daemon has no SIGTERM handler, so its server children are ours to reap
                os.killpg(self.daemon.pid, 9)
            except OSError:
                pass
            self.log.close()

    def events(self) -> list:
        out = []
        for f in sorted((self.pf / "events").glob("*")):
            out.extend(json.loads(line) for line in f.read_text().splitlines() if line.strip())
        return out

    def snapshot(self) -> dict:
        return {"claude": json.loads((self.home / ".claude.json").read_text()),
                "config": json.loads((self.pf / "config.json").read_text())}


def without(d: dict, path: tuple) -> dict:
    """`d` minus the key at `path` (deep copy of the rest)."""
    d = json.loads(json.dumps(d))
    node = d
    for k in path[:-1]:
        node = node.get(k, {})
    node.pop(path[-1], None)
    return d


def pending_install_rows(env: Env) -> list:
    rows = []
    for line in env.cli("approvals").stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "install_server":
            rows.append(parts[0])
    return rows


def attempt(env: Env, name: str) -> dict:
    before = env.snapshot()
    t0 = time.time()
    r = env.cli("install", name, timeout=200)
    result = {"name": name, "install_cli": (r.stdout + r.stderr).strip()[-300:], "check_s": round(time.time() - t0, 1)}
    rows = pending_install_rows(env)
    if not rows:
        result["outcome"] = "check_failed"
        return result
    approval = rows[-1]
    t1 = time.time()
    r = env.cli("approve", approval, timeout=200)
    result["approve_cli"] = (r.stdout + r.stderr).strip()[-300:]
    done = failed = None
    for _ in range(150):
        for e in env.events():
            if e["type"] == "install_done" and e.get("approval", e.get("id")) and e["ts"] >= t1 - 1:
                done = e
            if e["type"] == "install_failed" and e["ts"] >= t1 - 1:
                failed = e
        if done or failed:
            break
        time.sleep(2)
    result["install_s"] = round(time.time() - t1, 1)
    result["total_s"] = round(time.time() - t0, 1)
    steps = [e for e in env.events() if e["type"] == "install_step" and e["ts"] >= t1 - 1]
    result["steps"] = [{"step": e.get("step"), "ok": e.get("ok"), "at_s": round(e["ts"] - t1, 1)} for e in steps]
    if not done:
        result["outcome"] = "install_failed"
        result["reason"] = (failed or {}).get("reason")
        after = env.snapshot()
        result["rolled_back_clean"] = after == before
        return result
    install_id = done.get("id")
    result["install_id"] = install_id
    mid = env.snapshot()
    result["claude_added"] = install_id in mid["claude"].get("mcpServers", {})
    result["config_added"] = install_id in mid["config"].get("mcp_servers", {})
    grants_added = {k for k in mid["config"].get("grants", {}) if k.startswith(f"mcp__{install_id}__")}
    mid_config = without(mid["config"], ("mcp_servers", install_id))
    mid_config["grants"] = {k: v for k, v in mid_config.get("grants", {}).items() if k not in grants_added}
    result["others_preserved"] = (without(mid["claude"], ("mcpServers", install_id)) == before["claude"]
                                  and mid_config == before["config"])
    result["backup_exists"] = any(env.home.glob(".claude.json.pathfinder-*"))
    t2 = time.time()
    r = env.cli("uninstall", install_id, timeout=120)
    result["uninstall_cli"] = (r.stdout + r.stderr).strip()[-300:]
    time.sleep(2)
    after = env.snapshot()
    result["uninstall_s"] = round(time.time() - t2, 1)
    result["restored_claude"] = after["claude"] == before["claude"]
    result["restored_config"] = after["config"] == before["config"]
    result["outcome"] = "installed" if result["others_preserved"] else "installed_but_touched_others"
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=MAX_ATTEMPTS)
    ap.add_argument("--out", default=str(ROOT / "docs" / "measurements" / f"m5-{time.strftime('%Y-%m-%d')}.json"))
    args = ap.parse_args(argv)
    names = candidates()
    print(f"registry candidates: {len(names)}")
    env = Env()
    report = {"date": time.strftime("%Y-%m-%d"), "candidates_seen": len(names), "attempts": []}
    try:
        env.start()
        for name in names[: args.attempts]:
            print(f"-> {name}", flush=True)
            res = attempt(env, name)
            print(f"   {res['outcome']} in {res.get('total_s', res.get('check_s'))} s", flush=True)
            report["attempts"].append(res)
            if sum(1 for a in report["attempts"] if a["outcome"].startswith("installed")) >= 3:
                break
    finally:
        env.stop()
        shutil.rmtree(env.tmp, ignore_errors=True)
    ok = [a for a in report["attempts"] if a["outcome"] == "installed"]
    report["installed"] = len(ok)
    report["within_limit"] = sum(1 for a in ok if a["total_s"] <= LIMIT_S)
    report["rolled_back"] = sum(1 for a in ok if a["restored_claude"] and a["restored_config"])
    report["pass"] = report["installed"] >= 3 and report["within_limit"] >= 3 and report["rolled_back"] >= 3
    Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "attempts"}, indent=1))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
