"""Node providers: where remote node-agents come from (static VMs, Daytona, …).

See :mod:`mini_ork.remote.providers.base` for the contract and
``docs/architecture/cloud-sandbox-providers.md`` for the design. Importing this
package imports no provider module and no cloud SDK.
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
