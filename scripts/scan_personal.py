#!/usr/bin/env python3
"""Pre-commit scan: block identifying data before it reaches history.

Scans ADDED lines of the staged diff (or, with --all, every tracked file).
Reports file:line and the KIND of hit only - never echoes the matched value.
Run `python3 scripts/scan_personal.py --selfcheck` to test the patterns.
"""
import re
import subprocess
import sys

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com", "anthropic.com"}
PATTERNS = [
    ("api-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("home-path", re.compile(r"/(?:Users|home)/(?!\.\.\.|<|\$|USER\b|user\b|you\b|name\b)[A-Za-z0-9._-]+/")),
    ("phone-kr", re.compile(r"\b01[016789][- .]?\d{3,4}[- .]?\d{4}\b")),
]
# the last label must be alphabetic: an npm spec like `pkg@1.2.3` is not an address
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
BLOCKED_FILES = re.compile(r"(^|/)\.env(\..*)?$|\.(db|db-wal|db-shm|pem|p12|keychain)$|(^|/)credentials(\.|$)")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def owner_handles():
    """Account names to keep out of tracked files: git user.name and the remote's owner segment."""
    names = {git("config", "user.name").strip()}
    m = re.search(r"github\.com[:/]([^/]+)/", git("remote", "get-url", "origin"))
    if m:
        names.add(m.group(1))
    return {n.lower() for n in names if len(n) >= 4}


def hits_in(line, handles):
    kinds = [kind for kind, rx in PATTERNS if rx.search(line)]
    if any(d.lower() not in ALLOWED_EMAIL_DOMAINS for d in EMAIL.findall(line)):
        kinds.append("email")
    low = line.lower()
    if any(h in low for h in handles):
        kinds.append("owner-handle")
    return kinds


def staged_added_lines():
    path, lineno = None, 0
    for raw in git("diff", "--cached", "-U0", "--no-color").splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            lineno = int(re.search(r"\+(\d+)", raw).group(1)) - 1
        elif raw.startswith("+") and path:
            lineno += 1
            yield path, lineno, raw[1:]


def all_tracked_lines():
    for path in git("ls-files").splitlines():
        try:
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    yield path, n, line
        except (UnicodeDecodeError, OSError):
            continue


def selfcheck():
    h = {"somehandle"}
    bad = ["key " + "sk-" + "a" * 24, "mail someone" + "@" + "corp.co.kr", "/Us" + "ers/someone/x", "call 010" + "-1234-5678", "repo SomeHandle/thing"]
    ok = ["admin@example.com", "1+bot@users.noreply.github.com", "/Users/<name>/x", "~/.pathfinder/state.db", "version 0.1.0"]
    assert all(hits_in(s, h) for s in bad), [s for s in bad if not hits_in(s, h)]
    assert not any(hits_in(s, h) for s in ok), [s for s in ok if hits_in(s, h)]
    assert BLOCKED_FILES.search(".env") and BLOCKED_FILES.search("x/state.db") and not BLOCKED_FILES.search("docs/env.md")
    print("selfcheck ok")


def main():
    if "--selfcheck" in sys.argv:
        return selfcheck()
    handles = owner_handles()
    scan_all = "--all" in sys.argv
    files = git("ls-files").splitlines() if scan_all else git("diff", "--cached", "--name-only", "--diff-filter=AM").splitlines()
    problems = [f"{p}: blocked file type" for p in files if BLOCKED_FILES.search(p)]
    for path, n, line in (all_tracked_lines() if scan_all else staged_added_lines()):
        for kind in hits_in(line, handles):
            problems.append(f"{path}:{n}: {kind}")
    if problems:
        print("personal-data scan FAILED (values not shown):")
        print("\n".join("  " + p for p in problems))
        sys.exit(1)
    print(f"personal-data scan ok ({len(files)} files; emails, keys, home paths, phones, owner handle, blocked file types)")


if __name__ == "__main__":
    main()
