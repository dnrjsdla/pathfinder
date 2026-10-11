"""`pathfinder init` (onboarding) and `pathfinder service` (D15, D33, D36).

`run()` walks the checks in order, prints one line per check - `ok` or the exact fix line -
and stops at the first hard failure. Every write goes through `config.set` (the one config
writer), `merge_json_file` (the one foreign-file helper, config.py, D60) for
`~/.claude/settings.json`, or a small backup-then-append for `~/.zshrc` (the one foreign edit
that is not a JSON merge). `RULES.md`/`USER.md` are the daemon's first-start job, not this
module's. Every step is idempotent: running `init` twice changes nothing on the second run.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from .daemon import is_serving
from .config import backup_file, merge_json_file
from .ledger import Ledger

LAUNCH_LABEL = "pathfinder.daemon"

# The secret-free senses offered one yes/no each (D15); mail and calendar need a password or a
# system permission prompt, so they are only announced (see _announce_lines), never asked.
_ASKED_SENSES = ("attach", "catalog", "schedule", "reminders", "shell", "claude_code")
def _announce_lines():
    """One sense per line: `config set senses.NAME` keeps the others on."""
    mail = {"host": "imap.example.com", "user": "you@example.com"}
    return ("pathfinder config set senses.calendar '{}'",
            f"pathfinder config set senses.mail '{json.dumps(mail)}'")

# Copied verbatim from docs/senses.md's "Add to ~/.zshrc" block.
_ZSHRC_SNIPPET = '''zmodload zsh/datetime          # $EPOCHSECONDS is empty without it
autoload -Uz add-zsh-hook

_pathfinder_cmd_start=0

_pathfinder_preexec() {
  _pathfinder_cmd_start=$EPOCHSECONDS
  _pathfinder_cmd=$1
}

_pathfinder_precmd() {
  local exit=$?
  if [[ -n $_pathfinder_cmd ]]; then
    local seconds=$(( EPOCHSECONDS - _pathfinder_cmd_start ))
    local fields=$(printf '{"cmd": %s, "exit": %d, "seconds": %d, "cwd": %s}' \\
      "$(printf '%s' "$_pathfinder_cmd" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \\
      "$exit" "$seconds" \\
      "$(printf '%s' "$PWD" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")
    ( pathfinder signal shell "$fields" & ) >/dev/null 2>&1
    _pathfinder_cmd=""
  fi
}

add-zsh-hook preexec _pathfinder_preexec   # beside the user's own hooks, never over them
add-zsh-hook precmd _pathfinder_precmd
'''
_ZSHRC_LINE_MARKER = "hooks/pathfinder.zsh"

# Copied verbatim from docs/senses.md's Claude Code hooks JSON block. The marker is the argv
# construction, not a literal "pathfinder signal claude_code" string - the command builds that
# as a Python list (`['pathfinder','signal','claude_code', ...]`), so this is the substring that
# is actually contiguous in the copied text.
_CLAUDE_CODE_HOOK_MARKER = "'pathfinder','signal','claude_code'"
def _hook_entry(event):  # the docs/senses.md block, one entry per event
    return {"hooks": [{"type": "command", "command":
            "python3 -c \"import json,sys,subprocess; d=json.load(sys.stdin); "
            "subprocess.Popen(['pathfinder','signal','claude_code',json.dumps({'event':'" + event + "',"
            "'cwd':d.get('cwd',''),'message':d.get('message','')})])\""}]}


def _launchctl(argv):
    result = subprocess.run(["launchctl", *argv], capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _yes(answer, default):
    answer = (answer or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _fetch_model_ids(url):
    """None: the endpoint did not answer. []: it answered with no models."""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]


def _install_shell_hook(home_path):
    hooks_dir = home_path / "hooks"
    hooks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    zsh_path = hooks_dir / "pathfinder.zsh"
    zsh_path.write_text(_ZSHRC_SNIPPET, encoding="utf-8")
    os.chmod(zsh_path, 0o600)

    zshrc = Path(os.environ["HOME"]) / ".zshrc"
    guard = f'[ -f "{zsh_path}" ] && source "{zsh_path}"\n'
    if zshrc.exists():
        if _ZSHRC_LINE_MARKER in zshrc.read_text(encoding="utf-8"):
            return
        backup_file(zshrc)
        text = zshrc.read_bytes()
        with zshrc.open("a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith(b"\n") else "") + guard)
    else:
        zshrc.write_text(guard, encoding="utf-8")


def _has_pathfinder_hook(entries):
    return any(_CLAUDE_CODE_HOOK_MARKER in hook.get("command", "")
               for entry in entries for hook in entry.get("hooks", []))


def _mutate_claude_settings(d):
    hooks = d.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    notification = hooks.setdefault("Notification", [])
    if not _has_pathfinder_hook(stop):
        stop.append(_hook_entry("Stop"))
    if not _has_pathfinder_hook(notification):
        notification.append(_hook_entry("Notification"))
    return d


def _install_claude_code_hooks(say):
    path = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    try:
        merge_json_file(path, _mutate_claude_settings)
    except (ValueError, AttributeError, TypeError):  # not JSON, not an object, hooks not lists
        say(f"claude code hooks: {path} is not a JSON object with list hooks - fix it and run init again")
        return
    say("claude code hooks: ok")


def _plist_path():
    return Path(os.environ["HOME"]) / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _launchd_plist(home_path):
    interpreter_dir = str(Path(sys.executable).parent)
    path_value = ":".join([interpreter_dir, "/opt/homebrew/bin", "/usr/local/bin",
                           "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"PATH": path_value}
    if os.environ.get("PATHFINDER_HOME"):
        env["PATHFINDER_HOME"] = os.environ["PATHFINDER_HOME"]
    log = str(home_path / "daemon.log")
    return {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [sys.executable, "-m", "pathfinder", "daemon"],
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),  # launchd's cwd is "/": a clone needs this
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _install_launchd(home_path, launchctl):
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as f:
        plistlib.dump(_launchd_plist(home_path), f)
    return launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])


def run(home, *, ask=input, say=print, launchctl=_launchctl, models_url=None) -> int:
    home_path = Path(home)

    # 1. Python floor.
    py = f"python {sys.version_info[0]}.{sys.version_info[1]}"
    if sys.version_info < (3, 11):
        say(f"{py}: too old - brew install python@3.14")
        return 1
    say(f"{py}: ok")

    # 2. Ollama reachable, model (and optional embed_model) picked.
    cfg = config.load(home_path)
    url = models_url or cfg["providers"]["ollama"]["base_url"] + "/models"
    ids = _fetch_model_ids(url)
    if ids is None:
        say("ollama: not answering at " + url + " - run:")
        say("brew install ollama")
        say("brew services start ollama")
        say("ollama signin")
        return 1
    if not ids:
        say("ollama: no models - run:")
        say("ollama signin")
        say("ollama pull <a cloud model>")
        return 1
    ledger = Ledger(home_path)  # only touched once Ollama actually answered - see (1) above
    for i, model_id in enumerate(ids, 1):
        say(f"{i}. {model_id}")
    pick = ask("model [1]: ").strip()
    index = int(pick) - 1 if pick.isdigit() else 0
    if not 0 <= index < len(ids):
        index = 0
    config.set(home_path, "model", f"ollama:{ids[index]}", ledger=ledger)
    say(f"model: ollama:{ids[index]}")

    embed_candidates = [m for m in ids if "embed" in m.lower() or "bge" in m.lower()]
    if embed_candidates:
        yn = ask(f"use {embed_candidates[0]} for embeddings? [y/N]: ")
        if _yes(yn, default=False):
            config.set(home_path, "embed_model", f"ollama:{embed_candidates[0]}", ledger=ledger)
            say(f"embed_model: ollama:{embed_candidates[0]}")

    # 3. npx (warning only - the tool path needs it, the first conversation does not).
    if shutil.which("npx") is None:
        say("npx: not found - tool installs need it: brew install node")
    else:
        say("npx: ok")

    # 4. Home directory (RULES.md/USER.md are the daemon's job, not init's).
    home_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home_path, 0o700)
    say(f"home: {home_path}")

    # 5. Senses: one config.set for every secret-free yes; mail/calendar are announced only.
    senses = dict(config.load(home_path).get("senses") or {})
    for name in _ASKED_SENSES:
        if _yes(ask(f"{name} [Y/n]: "), default=True):
            senses.setdefault(name, {})
    config.set(home_path, "senses", senses, ledger=ledger)
    say("senses: " + ", ".join(sorted(senses)) + " - mail and calendar need one step each:")
    for line in _announce_lines():
        say(line)

    # 6. Shell hook.
    if "shell" in senses and _yes(ask("add one line to ~/.zshrc? [Y/n]: "), default=True):
        _install_shell_hook(home_path)
        say("shell hook: ok")

    # 7. Claude Code hooks.
    if "claude_code" in senses and _yes(ask("add Claude Code hooks? [Y/n]: "), default=True):
        _install_claude_code_hooks(say)

    # 8. launchd.
    if _yes(ask("start at login with launchd? [Y/n]: "), default=True):
        rc, out = _install_launchd(home_path, launchctl)
        deadline = time.monotonic() + 10
        while not is_serving(home_path) and time.monotonic() < deadline:
            time.sleep(0.5)
        if not is_serving(home_path):  # a re-run bootstraps an already loaded agent: rc != 0 but it answers
            say(f"launchd: the daemon did not answer - {out.strip() or rc}; see {home_path / 'daemon.log'}")
            return 1
        say("pathfinder tui")
    else:
        say("run the daemon by hand: pathfinder daemon")

    return 0


def service(home, action, launchctl=_launchctl) -> int:
    """`pathfinder service start|stop|restart|status`. The `launchctl` calls are one
    injectable function so tests never touch the real launchd."""
    domain = f"gui/{os.getuid()}"
    label = f"{domain}/{LAUNCH_LABEL}"
    if action == "start":
        rc, out = launchctl(["bootstrap", domain, str(_plist_path())])
    elif action == "stop":
        rc, out = launchctl(["bootout", label])
    elif action == "restart":
        rc, out = launchctl(["kickstart", "-k", label])
    elif action == "status":
        rc, out = launchctl(["print", label])
        if rc != 0:
            print("not loaded")
            return 1
        state = "not loaded" if "could not find service" in out.lower() else "loaded"
        pid_line = next((line.strip() for line in out.splitlines() if line.strip().startswith("pid = ")), "")
        socket_state = "answering" if is_serving(Path(home)) else "silent"
        print(f"{state}{' (' + pid_line + ')' if pid_line else ''}  socket: {socket_state}")
        return 0
    else:
        print(f"pathfinder: unknown service action {action!r}")
        return 1
    if rc != 0:
        print(f"pathfinder: launchctl failed: {out.strip() or rc}")
        return 1
    print("ok")
    return 0
