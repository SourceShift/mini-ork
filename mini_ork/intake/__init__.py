"""Work intake: a ticket lands, the agent starts.

The package that turns a project-management source into a running thread. It
sits on top of two things that already existed and that nothing had yet joined:
``mini_ork.pm`` (the provider-neutral work model, its registry and its mirror)
and ``mini_ork.threads`` (the durable, multi-turn unit of work).

Three modules, one job each:

``trigger``
    *When* a ticket becomes work — the rules, and the inert default.
``dispatch``
    *What happens* when it does — the ticket → thread → run path, the one place
    a ticket becomes a run, shared by both tiers.
``poll``
    *The loop* that tiers it — sync, filter, dispatch, repeat. The simple tier:
    outbound HTTPS only, no server, no public URL, no TLS.

``webhooks`` takes the same :func:`~mini_ork.intake.dispatch.dispatch_issue`
path from a signed inbound request instead of a timer; it lands with the rest
of the secure tier and changes nothing about the decision above it.

Submodules are **not** imported here. ``import mini_ork.intake`` should be
cheap and should not pull a database driver, a config file or the threads
service into a process that only wants, say, ``Trigger``.
"""
