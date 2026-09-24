"""The macOS Keychain, read-only. A secret never sits in the config file or the
ledger - this is the one place a secret enters the process."""
import subprocess


def secret(service: str, account: str) -> str | None:
    """The Keychain password for (service, account), or None when missing, wrong,
    or `security` is not on this machine."""
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):  # missing binary, or a Keychain prompt that hangs
        return None
    return result.stdout.strip() if result.returncode == 0 else None
