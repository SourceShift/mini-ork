"""mini-ork remote data-plane surface (epic 05 onward).

The node-agent HTTP server lives under :mod:`mini_ork.remote.node_agent`.
A separate top-level package keeps it isolated from the dispatch lanes
so it never imports from :mod:`mini_ork.dispatch.agents_config` or
:mod:`mini_ork.dispatch.providers` — those bind to the dispatcher
``.mini-ork/config/**`` shadow and must not surface in the agent process.
"""