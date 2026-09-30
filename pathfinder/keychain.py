"""The macOS Keychain. A secret never sits in the config file or the ledger - this is the one
place a secret enters or leaves the process."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

# One pattern for the ledger door and the pre-commit scan (D53): vendor-prefixed API keys and
# tokens, Notion's `ntn_`/`secret_`, and a private key's first line.
TOKEN_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
PASTED = "pathfinder-pasted"  # a pasted token's service; its account is its handle, pasted-N


def secret(service: str, account: str, run=None) -> str | None:
    """The Keychain password for (service, account), or None when missing, wrong,
    or `security` is not on this machine."""
    run = run or subprocess.run  # resolved here, so patching `subprocess.run` reaches it
    argv = ["security", "find-generic-password", "-s", service, "-a", account]
    try:
        result = run(argv + ["-w"], capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
        # `-w` prints a non-ASCII value as bare hex; only `-g`'s `password: 0x...` says that is
        # what happened - an all-hex ASCII token prints as `password: "..."` and is kept (5f)
        if result.returncode == 0 and re.fullmatch(r"(?:[0-9a-fA-F]{2})+", value):
            g = run(argv + ["-g"], capture_output=True, text=True, timeout=5)
            if g.returncode == 0 and "\npassword: 0x" in "\n" + g.stderr:
                return bytes.fromhex(value).decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, ValueError):  # missing binary, a prompt that hangs, not UTF-8
        return None
    return value if result.returncode == 0 else None


def store(service: str, account: str, value: str, run=None) -> None:
    """Add or replace the item. The command goes to `security -i` on stdin, so `ps` shows only
    `security -i`, never the value. An unescaped `"` would truncate the value and still exit 0,
    so the value is read back and compared (U0's measurements). Edge whitespace is dropped, as
    `secret()` drops it on every read."""
    run = run or subprocess.run
    value = value.strip()
    if not value:
        raise ValueError("an empty secret is not stored")
    if any(c in s for s in (service, account, value) for c in "\n\0"):
        raise ValueError("a secret cannot hold a newline or NUL")  # a newline would start a second command

    def quote(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    cmd = f"add-generic-password -U -s {quote(service)} -a {quote(account)} -w {quote(value)}\n"
    try:
        cmd.encode("utf-8")
    except UnicodeEncodeError:  # its `.object` is the whole command, value included
        cmd = ""
    if not cmd:  # raised outside the `except`, so no `__context__` carries the command
        raise ValueError("not valid text")
    result = run(["security", "-i"], input=cmd, capture_output=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"security could not store {account} for {service} (exit {result.returncode})")
    if secret(service, account, run) != value:
        raise RuntimeError(f"the Keychain gave back a different value for {account} in {service}")


def delete(service: str, account: str) -> bool:
    """Remove the item; True when it is gone (exit 44: it was never there)."""
    try:
        argv = ["security", "delete-generic-password", "-s", service, "-a", account]
        return subprocess.run(argv, capture_output=True, timeout=5).returncode in (0, 44)
    except (OSError, subprocess.SubprocessError):
        return False


def sweep(index: Path, now: float) -> dict:
    """Delete pasted items older than 24 hours from the Keychain and the index (D53); returns the
    index that is left, handle -> creation time. A delete the Keychain refuses stays for next time."""
    try:
        held = json.loads(index.read_text())
    except (OSError, ValueError):
        return {}
    live = {h: t for h, t in held.items() if now - t < 24 * 3600 or not delete(PASTED, h)}
    if live != held:
        _save(index, live)
    return live


def _save(index: Path, held: dict) -> None:
    """Handles and times, never values; replaced whole, so a crash never leaves half an index."""
    tmp = index.with_name(index.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(held, f)
    os.replace(tmp, index)


def _paste(token: str, index: Path) -> str:
    now = time.time()
    held = sweep(index, now)  # every new paste sweeps first
    account = f"pasted-{max((int(h.removeprefix('pasted-')) for h in held), default=0) + 1}"
    try:
        store(PASTED, account, token)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):  # locked, or no `security`
        return "[redacted]"
    _save(index, {**held, account: now})
    return f"[secret:{account}]"


def vault(event, index: Path):
    """`event` with each token-shaped substring stored as a pasted-N item and replaced by
    `[secret:pasted-N]`, or by `[redacted]` when the Keychain refuses it (D53). The same token
    twice in one event gets one handle. Only an event holding a token reaches the Keychain."""
    handles = {}

    def swap(m):
        if m[0] not in handles:
            handles[m[0]] = _paste(m[0], index)
        return handles[m[0]]

    def walk(v):
        if isinstance(v, str):
            return TOKEN_RE.sub(swap, v)
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        return [walk(x) for x in v] if isinstance(v, list) else v
    return walk(event)
