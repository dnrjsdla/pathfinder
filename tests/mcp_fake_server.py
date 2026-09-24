#!/usr/bin/env python3
"""A runnable stdio MCP fake server, for tests that need a real child process
(promoted from Mark.5's inline test script so later units can reuse it).

Speaks JSON-RPC over newline-delimited JSON on stdin/stdout. One tool, `echo`,
which takes `{"text": string}` and returns it as text content.

Flags:
  --stateless      answer `server/discover`, refuse `initialize` with a
                    JSON-RPC -32601 (method not found). Default (no flag) is
                    the legacy era: answer `initialize`, never answer
                    `server/discover` at all - real servers measured (2026-08-22)
                    just don't reply to it, so this fake matches that.
  --description TEXT  the `echo` tool's description (default below).
  --never-answer   read every request and never reply - for timeout tests.
"""
from __future__ import annotations

import argparse
import json
import sys

PROTOCOL_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-06-18"


def _tool(description: str) -> dict:
    return {
        "name": "echo",
        "description": description,
        "inputSchema": {"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
        "annotations": {"readOnlyHint": True},
    }


def _reply(mid, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def _error(mid, code, message) -> None:
    sys.stdout.write(json.dumps(
        {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stateless", action="store_true")
    parser.add_argument("--description", default="Echo the text back.")
    parser.add_argument("--never-answer", action="store_true")
    # `parse_known_args`: installer.py's `check()` now spawns this file directly as the "npx"/
    # "uvx" stand-in (tests/test_installer.py's `_WHICH`), so its argv also carries a realistic
    # `-y <identifier>@<version>` in front - ignored here rather than rejected.
    args, _ = parser.parse_known_args()
    tool = _tool(args.description)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if args.never_answer:
            continue  # read and drop, forever - the point of this flag
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue  # a notification; no reply

        if method == "server/discover":
            if args.stateless:
                _reply(mid, {"protocolVersions": [PROTOCOL_VERSION], "capabilities": {"tools": {}}})
            # legacy era: no reply at all, matching the real servers measured
            continue
        if method == "initialize":
            if args.stateless:
                _error(mid, -32601, "Method not found")
            else:
                _reply(mid, {"protocolVersion": LEGACY_VERSION, "capabilities": {},
                            "serverInfo": {"name": "fake", "version": "1"}})
            continue
        if method == "tools/list":
            _reply(mid, {"tools": [tool]})
            continue
        if method == "tools/call":
            said = (msg.get("params") or {}).get("arguments", {}).get("text", "")
            _reply(mid, {"content": [{"type": "text", "text": f"echo: {said}"}]})
            continue
        _error(mid, -32601, method)


if __name__ == "__main__":
    main()
