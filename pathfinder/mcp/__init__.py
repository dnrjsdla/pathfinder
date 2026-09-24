"""MCP client (transplanted from Mark.5 `brain/mcp/`, D1): the assistant buys tools other
people sell.

    servers.Servers          every configured server as one tool list  <- the outside knows this
      └ session.ServerSession   one server: discover / list / call
          └ transport.Transport    stdio or Streamable HTTP
              └ protocol            envelopes, errors, the `mcp__<server>__<tool>` naming rule
    pins.PinStore            pinned tool definitions - a changed description is held (rug pull)

This package knows nothing of `ToolSpec` or policy; `mcp/tools.py` is the one adapter.
Warnings and errors go to the stdlib logger `pathfinder.mcp`; the daemon attaches a handler
that records them as `error` events.
"""
