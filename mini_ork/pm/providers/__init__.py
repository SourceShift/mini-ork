"""Built-in PM sources.

Importing this package registers them, and that import-for-side-effect is the
single line a new source costs beyond its own module: the registry is then the
one list every surface reads. Keep registrations here rather than in
``pm/__init__.py`` so that "what sources exist" has exactly one answer.

Adding a source: write ``providers/<name>.py`` with a class, import it here,
call :func:`mini_ork.pm.registry.register_provider` on an instance.
"""

from mini_ork.pm.providers import local
from mini_ork.pm.registry import register_provider

#: mini-ork's own epics. The one source that needs no config, no network and no
#: credential, which is why it is registered first and unconditionally.
register_provider(local.provider())

__all__ = ["local"]
