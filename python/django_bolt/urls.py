"""Reverse-only URLconf for Django-Bolt routes.

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
"""

from __future__ import annotations

import re

from django.core.exceptions import ImproperlyConfigured
from django.urls import include
from django.urls import path as _path

from django_bolt.management.commands.runbolt import Command as _Runbolt

# Bolt path params use matchit ``{name}`` syntax. Only ``{name:path}`` is special
# (a catch-all that matches ``/``); the router ignores every other ``:type``, so we
# map catch-alls to Django's ``path`` converter and leave the rest untyped.
_PARAM_RE = re.compile(r"\{([^}:]+)(?::([^}]+))?\}")


def _to_django_route(bolt_path: str) -> str:
    """Convert a Bolt path to a Django route: ``/files/{p:path}`` -> ``files/<path:p>``."""

    def _convert(match: re.Match) -> str:
        name, converter = match.group(1), match.group(2)
        return f"<path:{name}>" if converter == "path" else f"<{name}>"

    return _PARAM_RE.sub(_convert, bolt_path).lstrip("/")


def _reverse_only_view(*args, **kwargs):  # pragma: no cover - never invoked
    raise RuntimeError("django_bolt reverse-only view should never be called")


def _iter_named_routes(api):
    """Yield ``(namespace, name, route, explicit)`` for every named route on ``api``."""
    routes = list(getattr(api, "_routes", []))
    routes += [(None, path, hid, fn) for path, hid, fn in getattr(api, "_websocket_routes", [])]
    for _method, full_path, handler_id, _fn in routes:
        meta = api._handler_meta.get(handler_id) or {}
        name = meta.get("name")
        if not name:
            continue
        yield meta.get("namespace") or "", name, _to_django_route(full_path), bool(meta.get("name_explicit"))


def build_urlpatterns(api) -> list:
    """Build reverse-only ``urlpatterns`` from a (merged) BoltAPI.

    Flat routes reverse by their bare name; routes whose API set a ``namespace``
    are grouped under it and reverse as ``namespace:name``, like a Django
    ``include`` with an ``app_name``. Identical targets (e.g. several methods on
    one path) are deduped, an explicit name wins over a derived one, and two
    explicit names mapping to different paths are a configuration error.
    """
    # (namespace, name) -> (route, explicit). Resolve priority/collisions here.
    chosen: dict[tuple[str, str], tuple[str, bool]] = {}
    for namespace, name, route, explicit in _iter_named_routes(api):
        key = (namespace, name)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = (route, explicit)
            continue
        prev_route, prev_explicit = existing
        if prev_route == route:
            continue
        if explicit and prev_explicit:
            label = f"{namespace}:{name}" if namespace else name
            raise ImproperlyConfigured(f"Duplicate route name {label!r} maps to both {prev_route!r} and {route!r}.")
        if explicit and not prev_explicit:
            chosen[key] = (route, True)

    flat: list = []
    namespaced: dict[str, list] = {}
    for (namespace, name), (route, _explicit) in chosen.items():
        pattern = _path(route, _reverse_only_view, name=name)
        (namespaced.setdefault(namespace, []) if namespace else flat).append(pattern)

    urlpatterns = list(flat)
    for namespace, patterns in namespaced.items():
        urlpatterns.append(_path("", include((patterns, namespace))))
    return urlpatterns


def _discover_urlpatterns() -> list:
    """Autodiscover Bolt APIs the same way ``runbolt`` does, then build their patterns."""
    command = _Runbolt()
    apis = command.autodiscover_apis()
    if not apis:
        return []
    return build_urlpatterns(command.merge_apis(apis))


urlpatterns = _discover_urlpatterns()
