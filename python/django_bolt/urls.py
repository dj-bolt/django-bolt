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
names from that mount, so this module leaves it out. An include of this
module under a prefix, such as ``path("api/", include("django_bolt.urls"))``,
moves the routes that it names under that prefix, so that ``reverse()``
gives the served paths.

``urlpatterns`` is a list with one include. Django reads the patterns in it
after it imports the whole URLconf, because the patterns depend on the
``api.urls`` mounts of the URLconf.
"""

from __future__ import annotations

from django.urls import path

from django_bolt._urlconf import ReverseOnlyURLConf, build_urlpatterns
from django_bolt.management.commands.runbolt import Command as _Runbolt

__all__ = ["build_urlpatterns", "urlpatterns"]

urlpatterns = [path("", (ReverseOnlyURLConf(_Runbolt), None, None))]
