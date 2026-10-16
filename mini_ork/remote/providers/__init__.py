"""Node providers: where remote node-agents come from (static VMs, Daytona, …).

See :mod:`mini_ork.remote.providers.base` for the contract and
``docs/architecture/cloud-sandbox-providers.md`` for the design. Importing this
package imports no provider module and no cloud SDK.

:mod:`~mini_ork.remote.providers.run_nodes` is deliberately NOT re-exported
here. It is pure stdlib plus the base contract, so importing it would be
harmless — but ``test_importing_the_package_loads_no_provider_module`` pins the
package's import surface to ``base`` alone, and that guard is worth more than
the convenience. ``from mini_ork.remote.providers import run_nodes`` still
works: the import system falls back to importing the submodule by name.
"""
from mini_ork.remote.providers.base import (
    DEFAULT_PROVIDER,
    ENV_PROVIDER,
    NodeEndpoint,
    NodeProvider,
    ProviderUnavailableError,
    acquire_run_node,
    attach_run_node,
    get_node_provider,
    known_node_providers,
    provider_name_for,
    read_run_node_record,
    reap_node_providers,
    register_lazy_node_provider,
    register_node_provider,
    release_run_node,
    resolve_provider_secret,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "ENV_PROVIDER",
    "NodeEndpoint",
    "NodeProvider",
    "ProviderUnavailableError",
    "acquire_run_node",
    "attach_run_node",
    "get_node_provider",
    "known_node_providers",
    "provider_name_for",
    "read_run_node_record",
    "reap_node_providers",
    "register_lazy_node_provider",
    "register_node_provider",
    "release_run_node",
    "resolve_provider_secret",
]
