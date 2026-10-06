"""Transport - how bytes move. **Does not know which era it speaks** (`Lifecycle`).

    Transport (abstract)
      |- StreamableHttpTransport   remote; where auth headers attach
      |- StdioTransport            local child process; a pipe is the door, not a key

Transport and protocol era are independent axes: stdio+legacy (most of today's
servers), http+stateless, and http+legacy all exist in the wild. That is why the
handshake lives in `session.py`, not here - this file only knows bytes.

What a transport knows: HTTP concerns (headers, SSE, status codes) and process
concerns (pipes, exit codes). What it does not know: MCP meaning - which tool is
dangerous, who may call it. Standard headers (`Mcp-Method`, `Mcp-Name`) are
derived from the message itself, so they are built here; `Mcp-Param-*` headers
need the tool's schema, so the session computes those and hands them down as
`extra_headers`.
"""
from __future__ import annotations

import json
import logging
import queue
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .protocol import (LEGACY_VERSION, META_VERSION, McpProtocolError,
                       McpTransportError, header_value, read_result)

_log = logging.getLogger("pathfinder.mcp")

# Methods where `params.name` (tool/prompt) or `params.uri` (resource) must be
# mirrored into the `Mcp-Name` header - the spec names these three.
_NAMED_METHODS = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}


class Transport(ABC):
    """One conversation channel with a server."""

    @abstractmethod
    def send(self, message: dict[str, Any], *,
             extra_headers: Mapping[str, str] | None = None,
             timeout: int = 30) -> dict[str, Any]:
        """Send one request and return its `result`.

        Raises `McpProtocolError` on a JSON-RPC `error`, `McpTransportError` if
        the server could not be reached at all. A failed tool call (`isError`)
        comes back as a **result, not an exception** - that split is the
        contract of this package.

        **A message with no `id` is a notification.** It is sent and an empty
        dict is returned; there is no separate method for this because every
        fake `Transport` would then need to implement a method it never uses.
        Notifications are already distinguished by shape in JSON-RPC, so the
        same distinction is used here.
        """

    def close(self) -> None:
        """Release any held resource. HTTP has none; stdio has a process."""


class StreamableHttpTransport(Transport):
    """Streamable HTTP under revision `2026-07-28`.

    This revision drops **both** the session and the GET stream. What is left
    is "one POST per request", and the reply is either a single JSON object or
    an SSE stream scoped to that one request - a client must accept **either**
    (spec MUST); which one the server sends is its own choice.
    """

    def __init__(self, endpoint: str, *,
                 headers: Mapping[str, str] | None = None) -> None:
        self._endpoint = endpoint
        # Where auth headers land. Never put into a log line or exception
        # message (see `_fail` below).
        self._static = dict(headers or {})

    def send(self, message: dict[str, Any], *,
             extra_headers: Mapping[str, str] | None = None,
             timeout: int = 30) -> dict[str, Any]:
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=self._headers_for(message, extra_headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                body = response.read()
        except urllib.error.HTTPError as exc:
            # The spec says errors also come back as a JSON-RPC body. Try that
            # first - it is usually more specific (e.g. which version it wants).
            raise self._from_error_body(exc) from exc
        except urllib.error.URLError as exc:
            raise McpTransportError(f"could not reach server: {exc.reason}") from exc
        except OSError as exc:
            raise McpTransportError(f"transport failed: {type(exc).__name__}: {exc}") from exc

        # Notifications get no reply. The spec says the server answers 202 with
        # an empty body.
        if "id" not in message:
            return {}
        if "text/event-stream" in content_type:
            payload = _final_message(body, message.get("id"))
        else:
            payload = _decode(body)
        return read_result(payload)

    # ---- internal -----------------------------------------------------------

    def _headers_for(self, message: dict[str, Any],
                     extra: Mapping[str, str] | None) -> dict[str, str]:
        """Derive the spec's required headers from the message.

        `MCP-Protocol-Version` must **match** the body's `_meta` value or the
        server rejects the request with 400 - so it is derived from the body,
        never set independently. No `_meta` means the legacy handshake already
        settled the version, so the version we proposed is used instead.
        """
        method = str(message.get("method", ""))
        meta = (message.get("params") or {}).get("_meta") or {}
        version = meta.get(META_VERSION) or LEGACY_VERSION
        headers = {
            "Content-Type": "application/json",
            # Both must be accepted - the server picks which one it replies with.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": str(version),
            "Mcp-Method": method,
        }
        key = _NAMED_METHODS.get(method)
        if key:
            value = (message.get("params") or {}).get(key)
            if value is not None:
                headers["Mcp-Name"] = header_value(str(value))
        headers.update(self._static)
        headers.update(extra or {})
        return headers

    def _from_error_body(self, exc: urllib.error.HTTPError) -> Exception:
        """Try to pull a JSON-RPC error out of an HTTP error response.

        Falls back to the bare status code. **The body never goes into the
        exception message whole** - some servers echo part of a token back in
        an auth failure response.
        """
        try:
            payload = _decode(exc.read())
            read_result(payload)          # raises if `error` is present
        except McpProtocolError:
            raise
        except Exception:
            pass
        return McpTransportError(f"HTTP {exc.code} {exc.reason}")


class StdioTransport(Transport):
    """Talks to a local child process over pipes - the `npx`/`uvx`-distributed
    servers.

    **The process starts lazily.** Something that opens a session per
    configured server must not wake all of them just by starting up.

    Note: this process runs **outside the sandbox**. `run_command`'s shell is
    kernel-confined, but an MCP server is third-party code running on the
    user's machine, and `npx -y <anything>` is arbitrary code execution. This
    class's own defenses are two: argv is exec'd directly (no shell), and the
    environment is an allow-list the caller builds and passes in. A third is
    tool-definition pinning (`pins.py`); there is no fourth.

    So the sentence that remains true: **listing a server in config = trusting
    that code.**
    """

    # Sentinel put on the reply queue when the server dies. `None` would be
    # indistinguishable from "nothing arrived yet".
    _EOF = object()

    def __init__(self, command: Sequence[str], *,
                 env: Mapping[str, str] | None = None,
                 cwd: str | Path | None = None) -> None:
        self._command = list(command)
        self._env = dict(env or {})
        self._cwd = str(cwd) if cwd else None
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()

    def send(self, message: dict[str, Any], *,
             extra_headers: Mapping[str, str] | None = None,
             timeout: int = 30) -> dict[str, Any]:
        """Write one line, read one line back (JSON-RPC over newline-delimited
        JSON).

        `extra_headers` is **dropped**. stdio has no headers - `x-mcp-header`
        mirroring is a stateless-HTTP requirement, and legacy stdio servers
        never see that annotation. Dropped silently, but noted here.

        ponytail: assumes one in-flight request at a time (no lock) - every
        caller today is the main thread. If a second thread starts calling the
        same server, `_await_reply` could steal another caller's answer; add a
        `threading.Lock` here when that happens.
        """
        proc = self._ensure_started()
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line)      # type: ignore[union-attr]
            proc.stdin.flush()          # type: ignore[union-attr]
        except (OSError, ValueError) as exc:
            raise McpTransportError(
                f"could not write to server (it may have died): {exc}") from exc

        if "id" not in message:
            return {}                   # notification - no reply expected
        return read_result(self._await_reply(message["id"], timeout))

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        alive = proc.poll() is None  # a leader reaped long ago may have had its pid reused (review 3)
        if alive:
            try:
                proc.stdin.close()              # type: ignore[union-attr]
            except (OSError, ValueError):
                pass
            # The spec's shutdown sequence: close stdin and let it exit on its own;
            # kill it if it doesn't.
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if alive:
            _kill_group(proc)  # a grandchild that outlived its parent would hold the pipes open (review)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):  # the pumps hit EOF once it is gone
            try:
                pipe.close()                # type: ignore[union-attr]
            except (OSError, ValueError, AttributeError):
                pass

    # ---- internal -----------------------------------------------------------

    def _ensure_started(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if self._proc is not None:
            raise McpTransportError(
                f"server has exited (code {self._proc.returncode}). "
                "Restart the assistant to reattach.")
        try:
            proc = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self._env, cwd=self._cwd,
                text=True, encoding="utf-8", bufsize=1,
                start_new_session=True)  # its own group: `npx` forks `node`, and close() must reach the grandchild
        except OSError as exc:
            raise McpTransportError(
                f"could not start server ({self._command[0]}): {exc}") from exc
        self._proc = proc
        threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return proc

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        """Read every line the server emits onto the reply queue.

        **Must never stop reading.** If the pipe buffer fills, the server
        blocks on its own write and the whole thing stalls - so this thread
        reads continuously rather than only when a reply is awaited.
        """
        try:
            for raw in proc.stdout:         # type: ignore[union-attr]
                line = raw.strip()
                if line:
                    self._replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._replies.put(self._EOF)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Send the server's log lines to the logger.

        **Must be read or the server stalls** (same reason as stdout). Not
        streamed straight to the terminal because that is third-party output -
        it could contain control characters and is not ours to show raw.
        """
        name = Path(self._command[0]).name
        try:
            for raw in proc.stderr:         # type: ignore[union-attr]
                line = raw.strip()
                if line:
                    _log.info(f"[mcp:stdio:{name}] {line[:500]}")
        except (OSError, ValueError):
            pass

    def _await_reply(self, request_id: Any, timeout: int) -> dict[str, Any]:
        """Find the reply to this specific request. **Keeps reading until the
        id matches.**

        Measurement required this: `server-everything` sent
        `notifications/tools/list_changed` **before** the `tools/list` reply,
        and a client that ignores `id` treats the notification as the answer.
        HTTP never had this problem (one request, one reply); `_final_message`
        does the same filtering for SSE.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTransportError(f"no reply within {timeout}s.")
            try:
                item = self._replies.get(timeout=remaining)
            except queue.Empty:
                raise McpTransportError(f"no reply within {timeout}s.") from None
            if item is self._EOF:
                code = self._proc.returncode if self._proc else "?"
                raise McpTransportError(f"server exited before replying (code {code}).")
            try:
                payload = json.loads(item)
            except json.JSONDecodeError:
                # Some servers violate the spec and print logs on stdout. Skip
                # the line and keep reading.
                _log.info(f"[mcp:stdio] dropped a non-JSON line: {item[:200]}")
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("id") != request_id:
                continue                    # a notification, or another request's reply
            return payload


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _decode(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpTransportError(f"could not read response as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise McpTransportError("response is not a JSON object.")
    return payload


def _final_message(body: bytes, request_id: Any) -> dict[str, Any]:
    """Find this request's final reply inside an SSE stream.

    Progress notifications (`notifications/progress`, etc.) may be interleaved
    before the final reply. We want only the one message that carries **our
    id** and a `result` or `error`; everything else is discarded. Lines
    starting with `:` are comments (keep-alives) per the spec and are ignored.
    """
    for chunk in _sse_data(body):
        try:
            message = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message
    raise McpTransportError("SSE stream had no final reply for this request.")


def _sse_data(body: bytes) -> list[str]:
    """Pull each event's `data` out of an SSE body (a blank line ends an event)."""
    events: list[str] = []
    buffer: list[str] = []
    for raw in body.decode("utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r")
        if not line:
            if buffer:
                events.append("\n".join(buffer))
                buffer = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        events.append("\n".join(buffer))
    return events
