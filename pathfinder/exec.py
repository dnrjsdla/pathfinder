"""The shell in a box: a macOS Seatbelt profile for `run_command`, plus the
process plumbing (timeout, interrupt, output clipping) around it.

The sandbox is an execution method, not a policy input - `policy.decide()` still
looks only at (tool, origin) and `escalate`. This is why the shell can run
without parsing: instead of telling `ls` from `rm -rf ~` in Python (a losing
game against `;`, backticks, `$( )`), both run, but the sandbox stops the
second one from deleting anything. The kernel does the telling.

SECRET_PATHS is the one list a leaking read could reach: it generates the
profile's deny-read rules AND is what `tools.read_file` checks before even
trying (reading happens in our own process, outside the sandbox, and a secret
in the model's context is one `curl` away from leaving)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from . import config

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

MAX_CHARS = 8000  # output clip: an unbounded `cat` could push the whole prompt out of cache

# Home-relative secrets. A directory is denied with everything under it; ".env"
# also covers ".env.local" etc. via regex (Seatbelt has no glob on file names).
SECRET_PATHS = (
    ".ssh", ".aws", ".gnupg", ".config/gh", ".docker",
    "Library/Keychains", "Library/Cookies",
    ".netrc", ".npmrc", ".git-credentials",
    ".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile",
    ".env",
)


def _deny_read_clauses() -> str:
    clauses = []
    for rel in SECRET_PATHS:
        if rel == ".env":
            clauses.append('(regex #"/\\.env$")')
            clauses.append('(regex #"/\\.env\\.[^/]+$")')
        else:
            clauses.append(f'(subpath (string-append (param "HOME") "/{rel}"))')
    return "\n  ".join(clauses)


# (deny default): an (allow default) profile with individual denies still let
# Apple Events through in earlier measurement - only deny-default actually locks
# app control down. Network is open (a shell without curl is not much of a
# shell); the price is that SECRET_PATHS is now the leak-prevention line itself,
# not a convenience. Writes are confined to the workspace and temp dirs.
_PROFILE = f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow file-read*)
(allow sysctl-read)
(allow signal (target self))
(allow network*)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.notification_center"))
(deny file-read*
  {_deny_read_clauses()})
(allow file-write*
  (subpath (param "WORKSPACE"))
  (subpath (param "TMPDIR"))
  (subpath "/private/tmp")
  (literal "/dev/null")
  (literal "/dev/stdout")
  (literal "/dev/stderr")
  (literal "/dev/tty")
  (regex #"^/dev/ttys[0-9]*$"))
"""

# Allow-list, not a deny-list: handing the sandboxed child the whole environment
# once leaked keys that no name-based deny-list would have guessed in advance.
CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")

BLOCKED_HINT = (
    "the sandbox may have blocked this (writes outside the workspace and app "
    "control are refused; network is open). Rewrite the command to stay inside "
    "the workspace, or ask again with escalate=true to run outside the sandbox "
    "(that needs the user's approval)."
)


def child_env(extra: dict | None = None) -> dict:
    """The environment a sandboxed child gets: only CHILD_ENV_KEYS, nothing else."""
    env = {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}
    env.update(extra or {})
    return env


def available() -> bool:
    """True when this machine can sandbox a shell at all."""
    return sys.platform == "darwin" and SANDBOX_EXEC.exists()


def is_secret(path) -> bool:
    """True when `path` is one of SECRET_PATHS (or an .env* file) under the
    user's home - the same set the sandbox profile denies. Used by
    `tools.read_file` to refuse before it ever opens the file itself."""
    home = Path.home()
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    if resolved.name == ".env" or resolved.name.startswith(".env."):
        return True
    for rel in SECRET_PATHS:
        if rel == ".env":
            continue
        candidate = (home / rel).resolve()
        if resolved == candidate or candidate in resolved.parents:
            return True
    return False


def workspace() -> Path:
    """The one place a sandboxed shell may write. Created on first use."""
    ws = config.home() / "workspace"
    ws.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ws, 0o700)
    return ws


def _wrap(command: str, ws: Path) -> list:
    """argv to run `command` inside the sandbox. The profile is inline (-p, no
    temp file); paths come in as -D parameters so they cannot escape the
    string literal by being pasted into the profile text."""
    return [
        str(SANDBOX_EXEC), "-p", _PROFILE,
        "-D", f"WORKSPACE={ws}",
        "-D", f"TMPDIR={Path(tempfile.gettempdir()).resolve()}",
        "-D", f"HOME={Path.home()}",
        "/bin/bash", "-c", command,
    ]


def _clip(text: str) -> tuple:
    if len(text) <= MAX_CHARS:
        return text, False
    head = tail = MAX_CHARS // 2
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...({dropped} characters omitted)...\n{text[-tail:]}", True


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_command(command: str, escalate: bool = False, *, control=None, timeout: int = 60) -> dict:
    """Run one command, cwd always the workspace.

    Whether this may run bare (no sandbox) was already decided by policy before
    this was ever called - `escalate` means the user approved it, and no
    sandbox on this machine means policy already turned the call into a write
    that needed approval too. This function just does what it is asked.

    A non-zero exit is an answer, not a failure (`grep` finding nothing exits
    1): `run_command` only reports ok=False upstream when it could not start
    the process at all.
    """
    ws = workspace()
    bare = escalate or not available()
    argv = ["/bin/bash", "-c", command] if bare else _wrap(command, ws)

    try:
        proc = subprocess.Popen(argv, cwd=ws, env=child_env(), start_new_session=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return {"exit_code": None, "stdout": "", "stderr": str(e), "sandboxed": not bare}

    # A watcher thread, not a timeout=... on communicate(): the process group
    # must die the moment control.interrupt is set, not only at the deadline.
    stop = threading.Event()

    def watch():
        while not stop.wait(0.05):
            if control is not None and control.interrupt.is_set():
                _kill_group(proc)
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        timed_out = True
    finally:
        stop.set()
        watcher.join()

    interrupted = control is not None and control.interrupt.is_set()
    out_text, cut_out = _clip(stdout or "")
    err_text, cut_err = _clip(stderr or "")
    result = {"exit_code": proc.returncode, "stdout": out_text, "stderr": err_text, "sandboxed": not bare}
    if cut_out or cut_err:
        result["truncated"] = True
    if timed_out or interrupted:
        result["killed"] = True
    # A sandbox block reads like a plain failure unless we say so - otherwise the
    # model just retries the same command.
    if not bare and proc.returncode and "not permitted" in err_text.lower():
        result["hint"] = BLOCKED_HINT
    return result
