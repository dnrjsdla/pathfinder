"""The macOS Keychain. A secret never sits in the config file or the ledger - this is the one
place a secret enters or leaves the process."""
from __future__ import annotations  # `str | None` on the system python3 (3.9), which runs the scanner

import json
import os
import re
import subprocess
import time
from pathlib import Path

# One pattern for the ledger door and the pre-commit scan (D53): a private key, PEM or PGP (the
# whole block when its END line comes before the next BEGIN; else the header and the base64 run
# after it, `\n` escapes included, as when the ledger's clip cut the END off), then
# vendor-prefixed keys and tokens, a JWT and a Telegram bot token - each only at the start of a
# word, so `task-management-...` is not an `sk-` key. It stays linear, since it runs under the
# ledger's lock: a body that could run on with no terminator either needs none, or starts only
# where its run of characters starts.
TOKEN_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:(?:(?!-----BEGIN )[\s\S])*?"
    r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|[A-Za-z0-9+/=\s:,\\-]*)"
    r"|(?<![A-Za-z0-9_-])(?:sk-(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{7,}\.eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{10,})"
    r"|(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ntn_[A-Za-z0-9]{20,}"
    r"|secret_[A-Za-z0-9]{40,}|[sr]k_(?:live|test)_[A-Za-z0-9]{20,}|\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-]))")
PASTED = "pathfinder-pasted"  # a pasted token's service; its account is its handle, pasted-<seq>[-i]
INDEX = "pasted.json"  # under the home: the ledger's `pasted`, handles and times only


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


def target(home, id: str, name: str) -> tuple:
    """`secret ID NAME`'s (service, account): an MCP server's variable, `pathfinder-mcp-<id>`/NAME."""
    if not valid_target(id, name):
        raise ValueError("secret ID NAME takes a server id and an env variable name")
    return f"pathfinder-mcp-{id}", name


def take(home, service: str, account: str, value: str, run=None) -> None:
    """A masked input's value (D53): the secret itself, or `--from pasted-<seq>[-i]` to bind a pasted
    item instead - the masked prompt lists the live handles."""
    flag, _, handle = value.strip().partition(" ")
    if flag == "--from":
        bind(home, handle.strip(), service, account, run)
    else:
        store(service, account, value, run)


def valid_target(id: str, name: str) -> bool:
    """`secret ID NAME`'s one check, for the shell and the screen alike: ID is a server id, NAME an
    env variable name."""
    from .config import _MCP_SERVER_ID
    return bool(_MCP_SERVER_ID.match(id)) and name.isidentifier() and name.isascii()


def delete(service: str, account: str, run=None) -> bool:
    """Remove the item; True when it is gone (exit 44: it was never there)."""
    try:
        argv = ["security", "delete-generic-password", "-s", service, "-a", account]
        return (run or subprocess.run)(argv, capture_output=True, timeout=5).returncode in (0, 44)
    except (OSError, subprocess.SubprocessError):
        return False


def _gone(account: str) -> bool:
    """True only when the Keychain answers that the pasted item is not there (exit 44): a locked
    keychain, or no `security`, never drops a handle. Asks without `-w`, so no value is read."""
    try:
        argv = ["security", "find-generic-password", "-s", PASTED, "-a", account]
        return subprocess.run(argv, capture_output=True, timeout=5).returncode == 44
    except (OSError, subprocess.SubprocessError):
        return False


def _walk(value, swap):
    if isinstance(value, str):
        return TOKEN_RE.sub(swap, value)
    if isinstance(value, dict):
        return {_walk(k, swap): _walk(v, swap) for k, v in value.items()}
    return [_walk(v, swap) for v in value] if isinstance(value, list) else value


def mask(value):
    """`value` (a text, or an event) with every token-shaped substring as `[redacted]`, and no
    Keychain call: for text the owner did not type (D66), and for a job's own copy of its goal."""
    return _walk(value, "[redacted]")


def sweep(index: Path, now: float) -> dict:
    """Delete pasted items older than 24 hours from the Keychain and the index, and drop a handle
    whose item is gone (D53, D66); returns the index left, handle -> creation time. A delete the
    Keychain refuses stays for next time. An index that is missing or unusable reads as empty, and
    an item whose handle it lost stays unknown (no item is ever found without its handle)."""
    held = _held(index)
    live = {h: t for h, t in held.items()  # expired: deleted; still young: asked whether it is there
            if not (delete(PASTED, h) if now - t >= 24 * 3600 else _gone(h))}
    if live != held:
        try:
            _save(index, live)
        except OSError:  # the deletes are done; the next sweep repeats them harmlessly
            pass
    return live


def _held(index: Path) -> dict:
    """The index, handle -> creation time."""
    try:
        return {str(h): float(t) for h, t in json.loads(index.read_text())["pasted"].items()}
    except Exception:  # missing, garbage, or the wrong shape: never a reason for an append to fail
        return {}


def bind(home, handle: str, service: str, account: str, run=None) -> None:
    """`secret <id> NAME --from <handle>` (D53): the pasted item moves to (service, account) - read,
    stored there, then deleted and its handle dropped from the index. A handle the index does not
    hold, or one 24 hours old, is expired (a seq never recurs, D66); the sweep deletes its item."""
    index = Path(home) / INDEX
    held = _held(index)
    value = secret(PASTED, handle, run) if time.time() - held.get(handle, 0) < 24 * 3600 else None
    if value is None:
        raise LookupError("expired - enter it again with /secret")
    store(service, account, value, run)
    if delete(PASTED, handle, run):  # a refused delete keeps the handle, so the sweep still finds the item
        held.pop(handle)
        _save(index, held)


def live(home) -> list:
    """The handles a masked prompt offers for `--from`, oldest first: younger than 24 hours, by the
    index alone - no Keychain call."""
    held = _held(Path(home) / INDEX)
    return sorted((h for h, t in held.items() if time.time() - t < 24 * 3600), key=held.get)


def _save(index: Path, held: dict) -> None:
    """Handles and times, never values; replaced whole, so a crash never leaves half an index."""
    tmp = index.with_name(index.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"pasted": held}, f)
    os.replace(tmp, index)


def handles(event) -> list:
    """The handles `vault` gave this event's own tokens - named by its seq (D66), so a handle the
    owner merely typed again is not one - for the owner's notice."""
    return re.findall(rf"\[secret:(pasted-{event['seq']}(?:-\d+)?)\]", str(event.get("text", "")))


def vault(event, index: Path):
    """`event` (the owner's own text, D66) with each token-shaped substring stored as an item
    named by the event's ledger seq - `pasted-<seq>`, then `pasted-<seq>-2`, ... - and replaced by
    `[secret:<handle>]`, or by `[redacted]` when the Keychain refuses it (D53). The seq never
    recurs, so a handle never comes to name another value. The same token twice gets one handle.
    Only an event holding a token reaches the Keychain, and it sweeps once."""
    handles, held = {}, None

    def swap(m):
        nonlocal held
        if m[0] in handles:
            return handles[m[0]]
        if held is None:
            held = sweep(index, time.time())
        account = f"pasted-{event['seq']}" + (f"-{len(handles) + 1}" if handles else "")
        if not _gone(account):  # every home shares the service: an item already there is never replaced
            handles[m[0]] = "[redacted]"
            return handles[m[0]]
        try:
            store(PASTED, account, m[0])
            held[account] = time.time()
            _save(index, held)
            handles[m[0]] = f"[secret:{account}]"
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):  # locked, or no `security`
            held.pop(account, None)
            delete(PASTED, account)  # a read-back that differed, or an unwritten index, leaves no orphan
            handles[m[0]] = "[redacted]"
        return handles[m[0]]
    return _walk(event, swap)
