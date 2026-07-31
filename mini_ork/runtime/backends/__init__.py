"""Optional ``Workspace``/``SharedDrive`` backends (sandbox P2+).

Each backend module registers itself with the runtime registries at import
(``register_workspace_backend`` / ``register_shared_drive_backend``). This
package is intentionally **not** imported by the default execution path — a
backend only becomes available when something explicitly imports its module
(e.g. ``import mini_ork.runtime.backends.docker``), which keeps the host
``local`` default free of any third-party/daemon dependency.
"""
