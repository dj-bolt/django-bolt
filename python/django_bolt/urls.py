"""Reverse-only URLconf for the Bolt routes that the URLconf does not mount.

Bolt routes live in the Rust matchit router, not in Django's URLconf, so
Django's ``reverse()`` can't see them. Include this module in your project's
``ROOT_URLCONF`` to register a reverse-only entry for every named Bolt route::

    # urls.py
    from django.urls import include, path

    urlpatterns = [
        path("", include("django_bolt.urls")),
        ...
    ]

With that in place, ``django.urls.reverse``/``reverse_lazy`` and the ``{% url %}``
template tag resolve Bolt route names natively -- converters, namespaces,
``args``/``kwargs`` and ``query``/``fragment`` all come from Django's own
resolver. The registered views never run (Bolt serves these paths in Rust);
they exist solely so Django can reverse the names.

Namespaces are opt-in, like Django's ``app_name``: pass ``namespace=`` to a
``BoltAPI`` and its routes reverse as ``reverse("<namespace>:<name>")``.

An API that the URLconf mounts with ``path("prefix/", api.urls)`` gets its
names from that mount, so this module leaves it out. Bolt serves these routes
at their own paths, so include this module with an empty prefix only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django_bolt._urlconf import build_urlpatterns, urlconf_mounts
from django_bolt.management.commands.runbolt import Command as _Runbolt

__all__ = ["build_urlpatterns", "urlpatterns"]


def _discover_urlpatterns() -> list:
    """Autodiscover Bolt APIs as ``runbolt`` does, then build the patterns of the APIs that the URLconf does not mount."""
    mounted = {id(mount.api) for mount in urlconf_mounts()}
    command = _Runbolt()
    apis = [(label, api) for label, api in command.autodiscover_apis() if id(api) not in mounted]
    if not apis:
        return []
    return build_urlpatterns(command.merge_apis(apis))


class _LazyUrlpatterns(Sequence):
    """The reverse-only patterns, built at the first read.

    Django reads them after it imports the whole URLconf. The URLconf can thus
    mount an API with ``api.urls`` after the include of this module.
    """

    def __init__(self) -> None:
        self._patterns: list | None = None

    def _load(self) -> list:
        if self._patterns is None:
            self._patterns = _discover_urlpatterns()
        return self._patterns

    def __getitem__(self, index: Any) -> Any:
        return self._load()[index]

    def __len__(self) -> int:
        return len(self._load())


urlpatterns = _LazyUrlpatterns()
