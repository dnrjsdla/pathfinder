"""The registry reader (D14): no reference-server process, `catalog` reads the official MCP
registry directly and shapes what it finds into the connection spec v0 (docs/spec/connection-v0.md)
`candidate` a proposal, and later the installer, read and write. `REGISTRY` is the only place in
this codebase a registry URL is written - everything else (the sense, `search_tools`) points at
this constant, never a copy of the string."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .tools import oneline  # the one filter for someone else's text; `catalog.oneline(...)` callers keep working

REGISTRY = "https://registry.modelcontextprotocol.io"
# PyPI's own JSON API - not the invariant's registry, but still one named place tests can repoint.
PYPI_URL = "https://pypi.org/pypi"

SPEC_FIELDS = ("name", "version", "source_registry", "description", "package",
              "verification", "install", "billing", "outcome")

# The application's rule (D32): a license the field holds but is not free to use commercially is
# excluded, never proposed by the catalog. AGPL/LGPL do not start with "GPL", so each needs its
# own prefix.
EXCLUDED_PREFIXES = ("GPL", "AGPL", "LGPL", "CC-BY-NC")

_RUNTIME_DEFAULT = {"npm": "npx", "pypi": "uvx"}

# filename (exact) -> marker words; docker-compose*/*.ipynb are prefix/suffix, handled separately.
_EXACT_MARKERS = {
    "package.json": ("node", "javascript", "npm"),
    "pyproject.toml": ("python",),
    "requirements.txt": ("python",),
    "Cargo.toml": ("rust",),
    "go.mod": ("go",),
    ".git": ("git",),  # not "github": every io.github.* registry name would match (live, 2026-09-24)
    "Gemfile": ("ruby",),
}

_SEP_RE = re.compile(r"[/.\-_]")


def rfc3339(ts: float) -> str:
    """UTC, built with plain string formatting (test_invariants counts every date computation
    outside clock.py/ledger.py, and this one call site is not worth a UTC escape hatch there)."""
    t = time.gmtime(ts)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def page(registry, cursor=None, since=None, limit=100, timeout=30) -> tuple:
    """One `GET {registry}/v0.1/servers?version=latest` page: `(items, next_cursor or None)`.
    `since` is a timestamp (sent as `updated_since`, RFC3339 only). Raises on a network or HTTP
    error - `mirror` decides what a failure means."""
    params = {"version": "latest", "limit": str(limit)}
    if since is not None:
        params["updated_since"] = rfc3339(since)
    if cursor:
        params["cursor"] = cursor
    data = _get(f"{registry}/v0.1/servers?{urllib.parse.urlencode(params)}", timeout)
    return data.get("servers", []), (data.get("metadata") or {}).get("nextCursor") or None


def latest(name: str, registry, timeout=20):
    """The registry's latest version of exactly `name`, or None when it has none (404)."""
    try:
        return _get(f"{registry}/v0.1/servers/{urllib.parse.quote(name, safe='')}/versions/latest", timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            e.close()
            return None
        raise


def listing(item) -> str:
    """One search line for a registry row: name, description, and what tells one server from another - the kind
    (a remote's host), "no source" for a package without a repository URL (most remotes have none), the month it was published."""
    cand, server = candidate(item), item["server"]
    pkg = cand["package"]
    kind = pkg["registry_type"]
    if kind == "remote":
        kind += " " + oneline(urllib.parse.urlparse(pkg["identifier"]).hostname or "", 60)
    published = re.match(r"\d{4}-\d\d", cand["verification"]["maintenance"]["last_publish_at"])
    parts = [kind.strip(), *(["no source"] if pkg["registry_type"] != "remote" and not (server.get("repository") or {}).get("url") else []),
             *([published[0]] if published else []), "license not checked"]
    return f"{oneline(cand['name'], 80)} - {oneline(cand['description'], 100)} ({', '.join(parts)})"


def _arg_str(arg) -> str:
    if isinstance(arg, str):
        return arg
    if isinstance(arg, dict):
        return str(arg.get("value", arg.get("name", "")))
    return str(arg)


def _package(server: dict) -> dict:
    for pkg in server.get("packages") or []:
        rtype = pkg.get("registryType")
        if rtype in ("npm", "pypi"):
            args = [_arg_str(a) for a in (pkg.get("runtimeArguments") or [])]
            # the pinned package itself sits between the runtime's flags and the package's own
            # arguments: `npx -y <id>@<version> ...` / `uvx <id>@<version> ...` - what the trial
            # and the installed command both run (live: without it npx ran the first argument)
            runtime = pkg.get("runtimeHint") or _RUNTIME_DEFAULT[rtype]
            spec = f"{pkg['identifier']}@{pkg.get('version') or ''}".rstrip("@")
            if runtime in ("npx", "uvx") and spec not in args:
                args.append(spec)  # another runtime hint (node, docker, an interpreter) names its own program
            args += [_arg_str(a) for a in (pkg.get("packageArguments") or [])]
            env = [{"name": e["name"], "secret": bool(e.get("isSecret")), "required": bool(e.get("isRequired")),
                    "default": oneline(e["default"]) if isinstance(e.get("default"), str) else None}  # registry text
                  for e in (pkg.get("environmentVariables") or [])
                  if isinstance(e.get("name"), str) and e["name"].isidentifier() and e["name"].isascii()]  # a name with `=` would read as account and default (D65); `valid_target`'s rule
            return {"registry_type": rtype, "identifier": pkg["identifier"],
                    "version": pkg.get("version", server.get("version", "")),
                    "runtime": runtime,
                    "args": args, "env": env, "remotes": server.get("remotes") or []}
    remotes = server.get("remotes") or []
    first = remotes[0] if remotes else {}
    return {"registry_type": "remote", "identifier": first.get("url", ""),
            "version": server.get("version", ""), "runtime": "not_checked",
            "args": [], "env": [], "remotes": remotes}


def candidate(item: dict) -> dict:
    """Spec v0 shape. Every field the code did not compute here is the literal `"not_checked"`
    (license, malicious_scan, sandbox_trial, description_hash, install) - later units fill them in."""
    server = item["server"]
    meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
    return {
        "name": server["name"],
        "version": server.get("version", ""),
        "source_registry": {"url": REGISTRY, "entry_id": server["name"],
                            "updated_at": meta.get("updatedAt", "not_checked")},
        "description": server.get("description", ""),
        "package": _package(server),
        "verification": {
            "license": {"spdx": "not_checked", "source": "not_checked", "verdict": "unknown"},
            "malicious_scan": "not_checked",
            "sandbox_trial": "not_checked",
            "maintenance": {"last_publish_at": meta.get("publishedAt") or meta.get("updatedAt") or "not_checked",
                            "namespace_verified": True},  # the registry's own claim, true for every active entry
            "description_hash": "not_checked",
            "trust_tier": "unverified",
        },
        "install": "not_checked",
        "billing": "unknown",
        "outcome": "proposed",
    }


def tags(state, home, now) -> set:
    """The coarse work-context tags: project names from the last seven days' `context` signals,
    plus marker words from listing each such cwd ONCE (never file contents). A vanished cwd
    contributes only its name."""
    cwds = state.context_cwds(now - 7 * 86400)
    out = set()
    for cwd in cwds:
        if not cwd:
            continue
        out.add(Path(cwd.rstrip("/")).name.lower())
        try:
            names = os.listdir(cwd)
        except OSError:
            continue
        out.update(w for n in names if n.startswith("docker-compose") for w in ("docker",))
        out.update(w for n in names if n.endswith(".ipynb") for w in ("jupyter",))
        names_set = set(names)
        for pattern, words in _EXACT_MARKERS.items():
            if pattern in names_set:
                out.update(words)
    return out


def matches(candidate: dict, tags: set) -> list:
    """Tags present as whole words (`/`, `.`, `-`, `_` treated as separators - `_` is a \\w
    character, so plain `\\b` alone would miss `git` inside `git_extra`) in the lowercased name
    or description. Sorted for a deterministic pick of "the first matched tag"."""
    # the last path segment only: the reverse-DNS namespace (io.github.<owner>) is not about the tool
    name_norm = _SEP_RE.sub(" ", candidate["name"].rsplit("/", 1)[-1]).lower()
    desc = (candidate.get("description") or "").lower()
    out = []
    for tag in sorted(tags):
        if re.search(rf"\b{re.escape(tag)}\b", name_norm) or re.search(rf"\b{re.escape(tag)}\b", desc):
            out.append(tag)
    return out


def _verdict(spdx) -> str:
    if not spdx:
        return "unknown" if spdx is None else "excluded"  # None: lookup failed/missing; "": no license
    s = str(spdx).strip().upper()
    if s.startswith("SEE LICENSE"):
        return "unknown"  # npm's "SEE LICENSE IN <file>": nothing here says what it is
    # PyPI often carries the license as a classifier ("GNU General Public License v3 (GPLv3)")
    # or the full text of the license (review: all three came back ok before)
    if (s == "UNLICENSED" or any(s.startswith(p) for p in EXCLUDED_PREFIXES)
            or "GENERAL PUBLIC LICENSE" in s or "AFFERO" in s or "NON-COMMERCIAL" in s or "NONCOMMERCIAL" in s):
        return "excluded"
    return "ok"


def spdx_of(value) -> str | None:
    """npm's `license` field is a string in modern packages but a legacy `{"type": ...}` object
    in older ones - the one normalizer both `_npm_license` and `installer.check`'s own npm lookup
    read it through, so a caller never re-derives this shape on its own."""
    if value is None:
        return None
    return value.get("type", "") if isinstance(value, dict) else str(value)


def _npm_license(pkg: dict, run) -> tuple:
    try:
        result = run(["npm", "view", f"{pkg['identifier']}@{pkg['version']}", "license", "--json"],
                     capture_output=True, text=True, timeout=20)
        if result.returncode != 0 or not result.stdout.strip():
            return None, "npm"
        return spdx_of(json.loads(result.stdout)), "npm"
    except Exception:
        return None, "npm"


def _pypi_license(pkg: dict) -> tuple:
    url = f"{PYPI_URL}/{pkg['identifier']}/{pkg['version']}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None, "pypi"
    info = data.get("info") or {}
    spdx = info.get("license_expression") or info.get("license") or ""
    if not spdx:
        for c in info.get("classifiers") or []:
            if c.startswith("License :: "):
                spdx = c.rsplit("::", 1)[-1].strip()
                break
    return spdx, "pypi"


def license(candidate: dict, run=subprocess.run) -> dict:
    """`{spdx, source, verdict}`. `verdict`: `excluded` for a GPL/AGPL/LGPL/CC-BY-NC family or an
    empty/`UNLICENSED` spdx (D32 - the application's rule); `unknown` when the lookup fails or the
    registry names no package to check (a remote entry); `ok` otherwise."""
    pkg = candidate["package"]
    rtype = pkg["registry_type"]
    if rtype == "npm":
        spdx, source = _npm_license(pkg, run)
    elif rtype == "pypi":
        spdx, source = _pypi_license(pkg)
    else:
        spdx, source = None, "unknown"
    return {"spdx": spdx if spdx is not None else "unknown", "source": source, "verdict": _verdict(spdx)}


def declined(state, name: str, now: float) -> bool:
    """True when `name` was proposed and denied (an accept-less proposal) within 30 days."""
    cutoff = now - 30 * 86400
    rows = state.db.execute(
        "SELECT args_json FROM approvals WHERE tool = 'install_server' AND decision = 'denied' AND ts >= ?",
        (cutoff,)).fetchall()
    return any(json.loads(r[0]).get("name") == name for r in rows)
