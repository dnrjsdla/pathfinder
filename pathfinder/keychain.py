"""The macOS Keychain. A secret never sits in the config file or the ledger - this is the one
place a secret enters or leaves the process."""
import re
import subprocess


def secret(service: str, account: str) -> str | None:
    """The Keychain password for (service, account), or None when missing, wrong,
    or `security` is not on this machine."""
    argv = ["security", "find-generic-password", "-s", service, "-a", account]
    try:
        result = subprocess.run(argv + ["-w"], capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
        # `-w` prints a non-ASCII value as bare hex; only `-g`'s `password: 0x...` says that is
        # what happened - an all-hex ASCII token prints as `password: "..."` and is kept (5f)
        if result.returncode == 0 and re.fullmatch(r"(?:[0-9a-fA-F]{2})+", value):
            g = subprocess.run(argv + ["-g"], capture_output=True, text=True, timeout=5)
            if g.returncode == 0 and "\npassword: 0x" in "\n" + g.stderr:
                return bytes.fromhex(value).decode("utf-8")
    except (OSError, subprocess.SubprocessError, ValueError):  # missing binary, a prompt that hangs, not UTF-8
        return None
    return value if result.returncode == 0 else None


def store(service: str, account: str, value: str, run=subprocess.run) -> None:
    """Add or replace the item. The command goes to `security -i` on stdin, so `ps` shows only
    `security -i`, never the value. An unescaped `"` would truncate the value and still exit 0,
    so the value is read back and compared (U0's measurements)."""
    if any(c in s for s in (service, account, value) for c in "\n\0"):
        raise ValueError("a secret cannot hold a newline or NUL")  # a newline would start a second command

    def quote(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    cmd = f"add-generic-password -U -s {quote(service)} -a {quote(account)} -w {quote(value)}\n"
    result = run(["security", "-i"], input=cmd, capture_output=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"security could not store {account} for {service} (exit {result.returncode})")
    if secret(service, account) != value:
        raise RuntimeError(f"the Keychain gave back a different value for {account} in {service}")
