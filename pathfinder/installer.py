"""Check, propose, install, roll back, uninstall an MCP server (D3/D4, D31-D33).

One `Installer`, owned by the daemon. `check()` runs the verification steps on a
worker thread and never raises - it fills the candidate's `verification`/`install`
blocks and stops at the first hard failure. `propose()` records the one approval
(D3); `decide()` is the `install_server` proposal handler; `install()` writes
PATHFINDER's own config and merges Claude Code's (D33), rolling back on any
failing step; `uninstall()` is the same reverse path from a completed install.

Every ledger write goes through `record`, marshalled by `dispatch` like
`jobs.Manager`/`memory.Manager` - `check()`/`install()`'s file and network work
run on a thread, off the daemon's event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from . import catalog, config, keychain, mirror
from .config import _atomic_write_json, backup_file, merge_json_file
from . import exec as exec_mod
from .mcp import pins as mcp_pins
from .mcp.protocol import qualify
from .mcp.servers import Entry, _resolve
from .mcp.session import ServerSession
from .mcp.tools import read_only, refused
from .mcp.transport import StdioTransport, StreamableHttpTransport
from .signals import Signal
from .tools import TOOLS as _TOOLS, visible

_log = logging.getLogger("pathfinder.installer")

# fix lines for a missing runtime (D3's check, step 1)
_RUNTIME_FIX = {"npx": "install Node.js (brew install node)", "uvx": "install uv (brew install uv)"}

# the runtime hint an npm/pypi package must launch through - anything else is the registry
# naming an arbitrary program to run (D3's check, step "version").
_EXPECTED_RUNTIME = {"npm": "npx", "pypi": "uvx"}
_ALLOWED_RUNTIME_FLAGS = {"npx": ("-y", "--yes")}
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.+-]*)?")


def _bad_runtime_args(pkg: dict, spec: str) -> str:
    """None-returning-"" on ok; else the `fail()` reason. The argv (`runtimeArguments` +
    the pinned spec + `packageArguments`, per `catalog._package`) is third-party text - only the
    exact pinned spec, preceded by nothing but the runtime's own known flags, is trusted to run."""
    args = pkg["args"]
    if spec not in args:
        return "package argument mismatch"
    allowed = _ALLOWED_RUNTIME_FLAGS.get(pkg["runtime"], ())
    for a in args[:args.index(spec)]:
        if a not in allowed:
            return f"unexpected runtime argument {a}"
    return ""


def _keychain_env(pkg: dict) -> dict:
    """Every secret or required variable (D40 - the registry omits `isSecret` when false, and some
    required tokens are not marked) as its config entry, U34's grammar for `mcp.servers._resolve`:
    `keychain:NAME` when required, `keychain?:NAME` when optional, then `=<default>` when the
    registry gives one (already `oneline`, by `catalog._package`)."""
    return {e["name"]: f"keychain{'' if e.get('required') else '?'}:{e['name']}"
            + (f"={e['default']}" if e.get("default") else "")
            for e in pkg["env"] if e.get("secret") or e.get("required")}

# The same alphabet as `senses.catalog._server_id` - kept in sync by hand there (a separate
# copy, not imported: that module imports this one for `propose_install`, so this one must not
# import back).
_SEG_RE = re.compile(r"[^a-z0-9-]+")


def _sanitize_id(name: str) -> str:
    seg = _SEG_RE.sub("-", name.rsplit("/", 1)[-1].lower()).strip("-")
    return seg[:32] or "server"


def server_id(name: str, existing) -> str:
    """The sanitized last path segment, `-2`/`-3` suffixed on a clash with `existing` - config's
    own id regex allows at most 32 characters."""
    base = _sanitize_id(name)
    if base not in existing:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = base[:32 - len(suffix)] + suffix
        if candidate not in existing:
            return candidate
        n += 1


def render_row(row: dict) -> str:
    """`pathfinder approvals`'s one line for an `install_server` proposal."""
    return f"{row['id']}  install_server  {brief(row['args'])}"


def brief(c: dict) -> str:
    """A candidate in one line - the approvals row and the gate line both show this."""
    lic = c["verification"]["license"]
    install = c.get("install") or {}
    integrity = "ok" if install.get("integrity") else "missing"
    trial = c["verification"].get("sandbox_trial") or {}
    tools = trial.get("tools") or []
    n, k = len(tools), sum(1 for t in tools if t.get("read_only_hint"))
    parts = [f"license {lic['spdx']} {lic['verdict']}", f"integrity {integrity}",
            f"trial ok, {n} tools ({k} read-only)"]
    if trial.get("skipped"):  # what the loader will refuse, its names escaped where the trial read them
        parts[-1] += f", {len(trial['skipped'])} skipped ({', '.join(trial['skipped'])[:120]})"
    if install.get("needs"):
        parts.append("needs: " + ", ".join(install["needs"]))
    if lic.get("warning"):
        parts.append("license warning")
    if install.get("runtime"):
        parts.append("command: " + " ".join([install["runtime"], *install.get("args", [])]))
    return f"{catalog.oneline(c['name'])}@{catalog.oneline(c['version'])}  " + catalog.oneline(" · ".join(parts))  # registry text: one clean line


def _npm_lookup(pkg: dict, run) -> tuple:
    """One `npm view` call for integrity + license together (never call npm twice for one
    candidate - the license verdict is computed from this value, not a second lookup)."""
    result = run(["npm", "view", f"{pkg['identifier']}@{pkg['version']}",
                 "dist.integrity", "dist.tarball", "license", "--json"],
                capture_output=True, text=True, timeout=20)
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    data = json.loads(result.stdout)
    return data.get("dist.integrity"), catalog.spdx_of(data.get("license"))


def _pypi_lookup(pkg: dict, urlopen) -> tuple:
    url = f"{catalog.PYPI_URL}/{pkg['identifier']}/{pkg['version']}/json"
    with urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    integrity = None
    for u in data.get("urls") or []:
        if u.get("packagetype") in ("sdist", "bdist_wheel"):
            integrity = (u.get("digests") or {}).get("sha256")
            if integrity:
                break
    info = data.get("info") or {}
    spdx = info.get("license_expression") or info.get("license") or ""
    if not spdx:  # the catalog's own order: expression, license, then a classifier
        spdx = next((c.rsplit("::", 1)[-1].strip() for c in info.get("classifiers") or [] if c.startswith("License :: ")), "")
    return integrity, spdx


def _claude_json_unchanged_except(before: dict, after: dict, id: str) -> bool:
    """Every key but `mcpServers[id]` deep-equals `before` - m5's own check. A missing
    `mcpServers` key in either side is treated the same as an empty one, so a fresh file (no
    prior `mcpServers`) is not flagged just for having one after this server is added; `id` is
    popped from both sides so the same helper checks an add (install) or a removal (uninstall)."""
    b, a = dict(before), dict(after)
    b_mcp, a_mcp = dict(b.pop("mcpServers", {}) or {}), dict(a.pop("mcpServers", {}) or {})
    b_mcp.pop(id, None)
    a_mcp.pop(id, None)
    return b == a and b_mcp == a_mcp


class Installer:
    def __init__(self, home, state, record, push_signal, attach, dispatch):
        self.home = Path(home)
        self.state = state
        self._record = record
        self.push_signal = push_signal
        self.attach = attach
        self.dispatch = dispatch
        self._lock = asyncio.Lock()  # S1: install/uninstall never interleave (id clash, config races)

    def record(self, type, **fields):
        self.dispatch(lambda: self._record(type, **fields))

    def _existing_ids(self) -> set:
        return set(config.load(self.home).get("mcp_servers") or {}) | self.state.installed_names()

    def _server_id(self, name: str) -> str:
        return server_id(name, self._existing_ids())

    # -- check -------------------------------------------------------------------------
    def check(self, candidate: dict, *, explicit: bool = False, run=None,
             urlopen=None, which=None, trial_timeout: int = 60) -> dict:
        # resolved here, not as default values - a default is bound once at import time, so
        # `mock.patch("pathfinder.installer.subprocess.run", ...)` would never reach it there.
        run = run or subprocess.run
        urlopen = urlopen or urllib.request.urlopen
        which = which or shutil.which
        pkg = candidate["package"]
        verification = candidate["verification"]

        def fail(step, reason):
            candidate["outcome"] = "install_failed"
            verification["failed_step"] = step
            verification["reason"] = catalog.oneline(reason, 300)  # registry text can ride in a reason
            return candidate

        rtype, runtime = pkg["registry_type"], pkg["runtime"]

        # 0. already installed under this name: a re-proposal is not a second copy (review, round
        # 3 - `server_id` would suffix `-2` and approve would install it). An upgrade is uninstall + install.
        for installed in self.state.installed_names():
            row = self.state.install(installed)
            if row and row["name"] == candidate["name"]:
                return fail("installed", f"already installed as {installed} - `pathfinder uninstall {installed}` first")

        # 1. runtime
        if rtype != "remote" and which(runtime) is None:
            fix = _RUNTIME_FIX.get(runtime, f"install {runtime}")
            return fail("runtime", f"{fix} - {runtime} not found")

        # 2. pinned version + integrity
        integrity, spdx = "not_checked", None
        if rtype in _EXPECTED_RUNTIME:
            expected = _EXPECTED_RUNTIME[rtype]
            if runtime != expected:
                return fail("version", f"unsupported runtime hint {runtime}")
            if not _VERSION_RE.fullmatch(pkg["version"] or ""):
                return fail("version", f"version must be pinned, got {pkg['version']}")
            bad = _bad_runtime_args(pkg, f"{pkg['identifier']}@{pkg['version']}")
            if bad:
                return fail("version", bad)
        if rtype == "npm":
            try:
                integrity, spdx = _npm_lookup(pkg, run)
            except Exception as e:
                return fail("version", f"npm view failed: {e}")
            if not integrity:
                return fail("version", f"could not resolve {pkg['identifier']}@{pkg['version']} on npm")
        elif rtype == "pypi":
            try:
                integrity, spdx = _pypi_lookup(pkg, urlopen)
            except Exception as e:
                return fail("version", f"PyPI lookup failed: {e}")
            if not integrity:
                return fail("version", f"could not resolve {pkg['identifier']}=={pkg['version']} on PyPI")

        # 3. license (from the value fetched above - never a second npm/PyPI call)
        verdict = catalog._verdict(spdx)
        verification["license"] = {"spdx": spdx if spdx else "unknown", "source": rtype, "verdict": verdict}
        if verdict == "excluded":
            if not explicit:
                return fail("license", f"license {spdx or 'unknown'} is excluded")
            verification["license"]["warning"] = True

        # 4. secrets (D40): every secret or required variable comes from the Keychain, read from the
        # very entry the install writes (U34) - a missing required one without a default stops here,
        # before the trial, all names at once (`names`, for the asking); a missing optional one
        # without a default is listed under `needs`
        sid = self._server_id(candidate["name"])
        resolved_env, missing, needs = _resolve(_keychain_env(pkg), sid, keychain.secret)
        if missing:
            verification["names"] = missing
            one = missing[0] if len(missing) == 1 else ""
            return fail("secrets", f"needs {', '.join(missing)} - store {'it' if one else 'each'} with /secret {sid} "
                        f"{one or 'NAME'} in the screen, or pathfinder secret {sid} {one or 'NAME'}, then ask "
                        "again; the value never goes through the chat")
        candidate["install"] = {"id": sid, "needs": needs}  # S2: decided once, here - never re-picked at install

        # 5. trial launch, in the sandbox
        try:
            tools, skipped = self._trial(sid, pkg, resolved_env, trial_timeout, which)
        except Exception as e:
            return fail("trial", f"{type(e).__name__}: {e}")
        if not tools:
            return fail("trial", "server started but listed zero tools")

        # 6. install block + description hash
        verification["sandbox_trial"] = {
            "ran_at": time.time(), "started": True, "tools": tools, "skipped": skipped,
            "file_writes_outside_tmp": False, "network": "not_checked", "verdict": "ok",
            "sandboxed": rtype != "remote" and exec_mod.available(),
        }
        prints = sorted(({"name": t["name"], "description": t["description_hash"], "schema": t["schema_hash"]}
                        for t in tools), key=lambda x: x["name"])
        verification["description_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(prints, sort_keys=True).encode()).hexdigest()
        verification["trust_tier"] = "checked"
        candidate["install"].update({
            "runtime": runtime, "pinned_version": pkg["version"], "integrity": integrity,
            # every secret or required variable, found or not (B6): the config references each as
            # `keychain:NAME`, so adding one later is all it takes - a required one keeps the server disabled until then
            "env_from_keychain": sorted(_keychain_env(pkg)),
            "args": list(pkg["args"]),
            "permissions": [{"tool": t["name"], "grant": "allow" if t["read_only_hint"] else "ask"} for t in tools],
        })
        return candidate

    def _trial(self, sid: str, pkg: dict, resolved_env: dict, timeout: int, which) -> list:
        args = list(pkg["args"])
        if pkg["registry_type"] == "remote":
            transport = StreamableHttpTransport(pkg["identifier"], headers=resolved_env)
            session = ServerSession(sid, transport, timeout=timeout)
            try:
                session.discover()
                raw = session.list_tools()
            finally:
                session.close()
        else:
            # resolved to the same binary step 1's `which` already found - never a bare name
            # handed to the OS's own PATH search a second time.
            runtime_bin = which(pkg["runtime"]) or pkg["runtime"]
            # resolved: Seatbelt matches real paths and macOS's temp dir is a /var -> /private/var symlink
            tmp = str(Path(tempfile.mkdtemp()).resolve())
            try:
                sandboxed = exec_mod.available()
                argv = [str(exec_mod.SANDBOX_EXEC), "-p", exec_mod._PROFILE, "-D", f"WORKSPACE={tmp}",
                       "-D", f"TMPDIR={tmp}", "-D", f"HOME={Path.home()}", runtime_bin, *args] \
                      if sandboxed else [runtime_bin, *args]
                env = exec_mod.child_env({"npm_config_cache": f"{tmp}/npm-cache",
                                          "UV_CACHE_DIR": f"{tmp}/uv-cache", **resolved_env})
                session = ServerSession(sid, StdioTransport(argv, env=env), timeout=timeout)
                try:
                    session.discover()
                    raw = session.list_tools()
                finally:
                    session.close()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        out, skipped = [], []
        for t in raw:
            name = t.get("name", "")
            entry = Entry(server_id=sid, tool_name=name, qualified_name=qualify(sid, name), definition=t)
            if refused(entry):
                skipped.append(visible(name))  # S5: never trialed further, never granted (the loader's own rule)
                continue
            prints = mcp_pins.fingerprint(t)
            out.append({"name": entry.tool_name, "description_hash": prints["description"],
                       "schema_hash": prints["inputSchema"], "read_only_hint": read_only(entry),
                       "params": mcp_pins.param_names(t)})
        return out, skipped

    # -- propose -------------------------------------------------------------------------
    def propose(self, candidate: dict, origin: str, session: str = "") -> str:
        if candidate.get("outcome") == "install_failed":
            vf = candidate["verification"]
            self.record("install_failed", id="", name=candidate["name"],
                        step=vf.get("failed_step", ""), reason=vf.get("reason", ""))
            signal = Signal(sense="catalog", kind="install_failed",
                key=f"install_failed:{candidate['name']}:{int(time.time())}",
                summary=f"Could not install {candidate['name']}: {vf.get('reason', '')}",
                fields={"name": candidate["name"], "step": vf.get("failed_step", "")},
                trust="self", exempt=True)
            self.dispatch(lambda: self.push_signal(signal))  # `propose` runs on the check's thread; the loop owns state
            return ""
        aid = uuid.uuid4().hex[:12]
        self.record("approval_pending", session=session, origin=origin, id=aid, call_id=aid,
                    tool="install_server", args=candidate, messages=[])
        return aid

    def propose_by_name(self, name: str, origin: str = "user", session: str = "",
                        registry: str | None = None) -> dict:
        items = mirror.find(self.home, name)
        if not items and (item := catalog.latest(name, registry or catalog.REGISTRY)):
            items = [item]  # newer than the last refresh, or the first sync still running
        matches = []
        for item in items:
            try:
                matches.append((catalog.candidate(item), item))
            except (KeyError, TypeError) as exc:
                _log.warning("propose_by_name: skipping malformed registry item: %s", exc)
        exact = [m for m in matches if m[0]["name"] == name]
        suffixed = [m for m in matches if m[0]["name"].endswith(f"/{name}")]
        if len(suffixed) > 1 and not exact:
            more = len(suffixed) - 10
            return {"error": "ambiguous: " + ", ".join(catalog.oneline(c["name"], 80) for c, _ in suffixed[:10])
                    + (f"; {more} more, give the full name" if more > 0 else "")}
        cand, item = (exact or suffixed or [(None, None)])[0]
        if cand is None:
            return {"error": "no such server; try search_tools"}
        if cand["name"] != catalog.oneline(cand["name"], 200):  # what is shown must be what is installed
            return {"error": "the registry name holds characters that cannot be shown; not proposed: "
                    + catalog.oneline(cand["name"], 80)}
        cand = self.check(cand, explicit=True)
        if cand.get("outcome") == "install_failed":
            self.propose(cand, origin, session)
            return {"error": cand["verification"].get("reason", "check failed")}
        aid = self.propose(cand, origin, session)
        line = render_row({"id": aid, "args": cand})
        status = ((item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}).get("status")
        return {"id": aid, "line": line + ("  deprecated in the registry" if status == "deprecated" else "")}

    async def handle(self, msg: dict) -> dict:
        install = msg["type"] == "install"
        arg = msg.get("name" if install else "id")
        if not isinstance(arg, str) or not arg:
            return {"type": "error", "message": "install needs a \"name\"" if install else "uninstall needs an \"id\""}
        try:
            result = await (asyncio.to_thread(self.propose_by_name, arg) if install else self.uninstall(arg))
        except Exception as e:  # a check or an uninstall that raised must answer, not drop the connection (m5: "no reply")
            result = {"error": f"{type(e).__name__}: {e}"[:300]}
        if "error" in result:
            return {"type": "error", "message": result["error"]}
        if install:
            return {"type": "install", "id": result["id"], "line": result["line"]}
        return {"type": "uninstall", "id": result["id"]}

    # -- install / rollback --------------------------------------------------------------
    async def decide(self, approval_id: str, decision: str) -> None:
        if decision == "approved":
            await self.install(approval_id)
        # denied: nothing - catalog.declined reads the `approval` event itself

    async def install(self, approval_id: str) -> None:
        row = self.state.approval(approval_id)
        if row is None:
            return
        candidate = row["args"]
        id = candidate["install"].get("id") or self._server_id(candidate["name"])
        async with self._lock:
            if id in (config.load(self.home).get("mcp_servers") or {}):
                # N1: a second proposal for a name already installed must not touch the live
                # server (its row, its session, its tools) - refuse before any step is recorded
                reason = f"server id already taken: {id}"
                self.record("install_failed", id="", name=candidate["name"], step="mcp_servers", reason=reason)
                self.push_signal(Signal(sense="catalog", kind="install_failed", key=f"install_failed:{id}:{int(time.time())}",
                    summary=f"Could not install {candidate['name']}: {reason}", fields={"id": id, "step": "mcp_servers"},
                    trust="self", exempt=True))
                return
            outcome = await asyncio.to_thread(self._install_steps, id, candidate)
            if outcome["ok"]:
                try:
                    await self.attach.reload(id)
                except Exception as e:
                    outcome["ok"], outcome["step"], outcome["reason"] = False, "load", f"{type(e).__name__}: {e}"
                else:
                    outcome["ok"], outcome["step"], outcome["reason"] = self._check_loaded(id, candidate)
        if outcome["ok"]:
            tools = candidate["verification"]["sandbox_trial"]["tools"]
            names = [t["name"] for t in tools]
            allowed = sum(1 for t in tools if t["read_only_hint"])
            self.record("install_done", id=id, name=candidate["name"], version=candidate["version"],
                        tools=names, backup=outcome["backup"])
            self.push_signal(Signal(sense="catalog", kind="install_done", key=f"install_done:{id}",
                summary=f"Installed {candidate['name']}@{candidate['version']}: {len(names)} tools ({allowed} allowed)",
                fields={"id": id, "tools": names}, trust="self", exempt=True))
        else:
            try:
                self._rollback(id, outcome)
            finally:
                self.record("install_failed", id=id, name=candidate["name"],
                            step=outcome["step"], reason=outcome["reason"])
                self.push_signal(Signal(sense="catalog", kind="install_failed",
                    key=f"install_failed:{id}:{int(time.time())}",
                    summary=f"Could not install {candidate['name']}: {outcome['reason']}",
                    fields={"id": id, "step": outcome["step"]}, trust="self", exempt=True))

    def _check_loaded(self, id: str, candidate: dict) -> tuple:
        """B5/B6: the load step can fail - `attach.reload` not raising is not enough, a server
        that dies (or never listed a tool) must not become `install_done`. The one exception is
        a server whose only problem is a secret the proposal already listed under `needs`
        (Mark.5's rule): that one stays `disabled` until the user adds it, step `load` records ok."""
        if any(name.startswith(f"mcp__{id}__") for name in _TOOLS):
            self.record("install_step", id=id, step="load", ok=True, detail="")
            return True, "", ""
        reason = dict(self.attach.servers.disabled()).get(id, "")
        if reason.startswith("secret missing") and candidate["install"].get("needs"):
            self.record("install_step", id=id, step="load", ok=True, detail=f"disabled: {reason}")
            return True, "", ""
        detail = f"disabled: {reason}" if reason else "no tools registered"
        self.record("install_step", id=id, step="load", ok=False, detail=detail)
        return False, "load", detail

    def _install_steps(self, id: str, candidate: dict) -> dict:
        pkg, install = candidate["package"], candidate["install"]
        pre_servers = config.load(self.home).get("mcp_servers") or {}
        pre_grants = config.load(self.home).get("grants") or {}
        out = {"ok": False, "done": [], "reason": "", "step": "", "backup": "",
              "pre_servers": pre_servers, "pre_grants": pre_grants}

        if id in pre_servers:  # S2: the id was decided once, at check - a clash fails, never a silent -2
            reason = f"server id already taken: {id}"
            self.record("install_step", id=id, step="mcp_servers", ok=False, detail=reason)
            return {**out, "reason": reason, "step": "mcp_servers"}

        env_cfg = _keychain_env(pkg)
        if pkg["registry_type"] == "remote":
            server_cfg = {"transport": "http", "url": pkg["identifier"], "headers": env_cfg}
        else:
            server_cfg = {"transport": "stdio", "command": [install["runtime"], *install["args"]], "env": env_cfg}
        try:
            config.set(self.home, "mcp_servers", {**pre_servers, id: server_cfg})
            self.record("install_step", id=id, step="mcp_servers", ok=True, detail=id)
            out["done"].append("mcp_servers")
        except Exception as e:
            self.record("install_step", id=id, step="mcp_servers", ok=False, detail=str(e))
            return {**out, "reason": str(e), "step": "mcp_servers"}

        try:
            backup = self._merge_claude_json(id, server_cfg)
            out["backup"] = backup
            self.record("install_step", id=id, step="claude_code", ok=True, detail=backup)
            out["done"].append("claude_code")
        except Exception as e:
            self.record("install_step", id=id, step="claude_code", ok=False, detail=str(e))
            return {**out, "reason": str(e), "step": "claude_code"}

        try:
            for t in candidate["verification"]["sandbox_trial"]["tools"]:
                self.record("pin_accept", server=id, tool=t["name"], description_hash=t["description_hash"],
                            schema_hash=t["schema_hash"], params=t.get("params", []))
            self.record("install_step", id=id, step="pins", ok=True, detail="")
            out["done"].append("pins")
        except Exception as e:
            self.record("install_step", id=id, step="pins", ok=False, detail=str(e))
            return {**out, "reason": str(e), "step": "pins"}

        try:
            allow = {f"mcp__{id}__{p['tool']}": "allow" for p in install["permissions"] if p["grant"] == "allow"}
            if allow:
                config.set(self.home, "grants", {**pre_grants, **allow})
            self.record("install_step", id=id, step="grants", ok=True, detail=str(len(allow)))
            out["done"].append("grants")
        except Exception as e:
            self.record("install_step", id=id, step="grants", ok=False, detail=str(e))
            return {**out, "reason": str(e), "step": "grants"}

        out["ok"] = True
        return out

    def _merge_claude_json(self, id: str, server_cfg: dict) -> str:
        """Reads/writes `~/.claude.json` (D33, user scope) through `merge_json_file` (D36).
        Returns the backup path, or "" when the file did not exist before (nothing to restore
        to but deletion)."""
        path = Path(os.environ.get("HOME", str(Path.home()))) / ".claude.json"
        before = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if server_cfg["transport"] == "stdio":
            entry = {"command": server_cfg["command"][0], "args": list(server_cfg["command"][1:]),
                     "env": {name: f"${{{name}}}" for name in server_cfg["env"]}}
        else:
            entry = {"type": "http", "url": server_cfg["url"],
                     "headers": {name: f"${{{name}}}" for name in server_cfg["headers"]}}

        def mutate(d: dict) -> dict:
            d["mcpServers"] = {**(d.get("mcpServers") or {}), id: entry}
            return d

        backup = merge_json_file(path, mutate)
        readback = json.loads(path.read_text(encoding="utf-8-sig"))
        if not _claude_json_unchanged_except(before, readback, id):
            raise RuntimeError("claude.json merge changed an unrelated key")
        return backup

    def _unload(self, id: str) -> None:
        self.attach.servers.unload(id)
        for name in [n for n in _TOOLS if n.startswith(f"mcp__{id}__")]:
            del _TOOLS[name]

    def _rollback(self, id: str, outcome: dict) -> None:
        """Every step in its own try/except (B7) - one failing restore must not stop the rest
        from being attempted."""
        for step in reversed(outcome["done"]):
            try:
                if step == "grants":
                    config.set(self.home, "grants", outcome["pre_grants"])
                elif step == "pins":
                    self.record("pin_revoke", server=id)
                elif step == "claude_code":
                    # not the backup: the load step (a first npx download) sits between backup and
                    # rollback, and Claude Code may have written meanwhile (review 3)
                    self._remove_from_claude_json(id)
                elif step == "mcp_servers":
                    config.set(self.home, "mcp_servers", outcome["pre_servers"])
            except Exception as e:
                self.record("install_step", id=id, step=f"rollback_{step}", ok=False, detail=str(e))
        if "mcp_servers" in outcome["done"]:  # nothing of ours was loaded otherwise (N1)
            self._unload(id)

    # -- uninstall -------------------------------------------------------------------------
    async def uninstall(self, id: str) -> dict:
        """The reverse of `install` from a completed (or partially rolled-forward, B7) install -
        never the install-time backup (B1: that backup is `rollback`'s alone, seconds after it was
        taken; restoring it here would wipe every later write to `~/.claude.json`, another
        server's entry or Claude Code's own state, included). Reads the CURRENT file instead, pops
        this server's own entry, writes atomically, read-back deep-compares every other key.
        Order: `mcp_servers` through `config.set`, then `grants`, then the foreign file, then
        pins/unload, then `uninstalled` - each step guarded so a raise records what happened and
        stops there; the row then reads `installing` (B7 treats that like `installed` for a retry
        of this same reverse path)."""
        row = self.state.install(id)
        if row is None or row["state"] not in ("installed", "installing"):
            return {"error": f"no installed server: {id}"}
        async with self._lock:
            return await asyncio.to_thread(self._uninstall_steps, id, row)

    def _uninstall_steps(self, id: str, row: dict) -> dict:
        cfg = config.load(self.home)
        try:
            servers = {k: v for k, v in (cfg.get("mcp_servers") or {}).items() if k != id}
            if len(servers) != len(cfg.get("mcp_servers") or {}):
                config.set(self.home, "mcp_servers", servers)
            self.record("install_step", id=id, step="mcp_servers", ok=True, detail="")
        except Exception as e:
            self.record("install_step", id=id, step="mcp_servers", ok=False, detail=str(e))
            return {"error": str(e)}

        try:
            grants = {k: v for k, v in (cfg.get("grants") or {}).items() if not k.startswith(f"mcp__{id}__")}
            if len(grants) != len(cfg.get("grants") or {}):
                config.set(self.home, "grants", grants)
            self.record("install_step", id=id, step="grants", ok=True, detail="")
        except Exception as e:
            self.record("install_step", id=id, step="grants", ok=False, detail=str(e))
            return {"error": str(e)}

        try:
            self._remove_from_claude_json(id)
            self.record("install_step", id=id, step="claude_code", ok=True, detail="")
        except Exception as e:
            self.record("install_step", id=id, step="claude_code", ok=False, detail=str(e))
            return {"error": str(e)}

        try:
            self._unload(id)
            self.record("pin_revoke", server=id)
            self.record("install_step", id=id, step="pins", ok=True, detail="")
        except Exception as e:
            self.record("install_step", id=id, step="pins", ok=False, detail=str(e))
            return {"error": str(e)}

        self.record("uninstalled", id=id, name=row["name"])
        return {"id": id}

    def _remove_from_claude_json(self, id: str) -> None:
        """Pops `mcpServers[id]` from the CURRENT file (B1/B2) - never the install-time backup.
        A missing file, or one that never held this id, is a no-op (never raises)."""
        path = Path(os.environ.get("HOME", str(Path.home()))) / ".claude.json"
        if not path.exists():
            return
        before = json.loads(path.read_text(encoding="utf-8-sig"))
        if id not in (before.get("mcpServers") or {}):
            return
        after = dict(before)
        after["mcpServers"] = {k: v for k, v in before["mcpServers"].items() if k != id}
        if not after["mcpServers"]:
            del after["mcpServers"]  # a key this path created for its only entry goes with it
        _atomic_write_json(path, after)
        readback = json.loads(path.read_text(encoding="utf-8-sig"))
        if not _claude_json_unchanged_except(before, readback, id):
            raise RuntimeError("claude.json removal changed an unrelated key")


# -- the tool (senses/catalog.py's TOOLS, opt-in per the sense being on) ---------------------

_installer: "Installer | None" = None


def bind(installer: "Installer") -> None:
    global _installer
    _installer = installer


def propose_install_by_name(name: str, session: str = "", registry: str | None = None) -> str:
    if _installer is None:
        raise RuntimeError("installer not bound (no daemon)")
    result = _installer.propose_by_name(name, origin="user", session=session, registry=registry)
    if result.get("error"):
        raise LookupError(result["error"])  # a failed proposal is a failed call
    return f"proposed as {result['id']}: {result['line']}"
