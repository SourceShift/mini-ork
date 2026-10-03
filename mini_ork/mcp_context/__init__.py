"""``mini-ork mcp-context`` — read-only MCP server exposing runs/learnings/cost/lanes.

The package holds the stdio JSON-RPC 2.0 implementation
(:mod:`mini_ork.mcp_context.server`) and is invoked by the thin CLI
:mod:`mini_ork.cli.mcp_context_cmd`. Nothing in this package writes; the
``StateDB`` is opened read-only by :func:`mini_ork.web.deps.db_for` so
even an accidental ``UPDATE`` would be refused at the SQLite layer.
"""