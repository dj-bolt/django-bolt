"""
Synthetic urlconf for Bolt routes

Referenced by name from ``django_bolt.urls``
"""

from __future__ import annotations

import re

from django.core.exceptions import ImproperlyConfigured
from django.urls import include
from django.urls import path as _django_path

from django_bolt.management.commands.runbolt import Command as _Runbolt

# Bolt path params use matchit syntax ``{name}``; Django paths use ``<name>``.
_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _to_django_route(bolt_path: str) -> str:
    """Convert a Bolt path to a Django route: ``/missions/{id}`` -> ``missions/<id>``."""
    return _PARAM_RE.sub(r"<\1>", bolt_path).lstrip("/")


def _reverse_only_view(*args, **kwargs):  # pragma: no cover - never invoked
    """Placeholder view; the synthetic urlconf exists purely for reversing."""
    raise RuntimeError("django_bolt reverse-only view should never be called")


def _iter_named_routes(merged):
    """Yield (namespace, name, route, explicit) for every named merged route."""
    routes = list(getattr(merged, "_routes", []))
    routes += [(None, p, hid, fn) for p, hid, fn in getattr(merged, "_websocket_routes", [])]
    for _method, full_path, handler_id, _fn in routes:
        meta = merged._handler_meta.get(handler_id) or {}
        name = meta.get("name")
        if not name:
            continue
        yield (
            meta.get("namespace") or "",
            name,
            _to_django_route(full_path),
            bool(meta.get("name_explicit")),
        )


def _build_urlpatterns(merged) -> list:
    """Build synthetic ``urlpatterns`` from a merged BoltAPI.

    One ``path()`` per named route, grouped into namespaces via ``include``.
    Explicit names win over derived ones that resolve to the same name; two
    explicit names that collide to different paths are a configuration error.
    """
    # (namespace, name) -> (route, explicit). Resolve priority/collisions here.
    chosen: dict[tuple[str, str], tuple[str, bool]] = {}
    for namespace, name, route, explicit in _iter_named_routes(merged):
        key = (namespace, name)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = (route, explicit)
            continue
        prev_route, prev_explicit = existing
        if prev_route == route:
            continue  # same target (e.g. multiple methods / HEAD mirror) — dedupe
        if explicit and prev_explicit:
            label = f"{namespace}:{name}" if namespace else name
            raise ImproperlyConfigured(
                f"Duplicate explicit route name {label!r} maps to both "
                f"{prev_route!r} and {route!r}."
            )
        if explicit and not prev_explicit:
            chosen[key] = (route, True)  # explicit overrides a derived name
        # else: keep the existing (explicit, or first-seen derived)

    by_namespace: dict[str, list] = {}
    for (namespace, name), (route, _explicit) in chosen.items():
        by_namespace.setdefault(namespace, []).append(
            _django_path(route, _reverse_only_view, name=name)
        )

    urlpatterns: list = []
    for namespace, patterns in by_namespace.items():
        if namespace:
            urlpatterns.append(_django_path("", include((patterns, namespace))))
        else:
            urlpatterns.extend(patterns)
    return urlpatterns


def _find_django_mount_prefix(merged) -> str | None:
    """Prefix a mounted Django app (``mount_django``) is served under, or None.

    Django's ``ROOT_URLCONF`` is only reachable through the mount in a Bolt
    process, so reversed Django names need this prepended. Returns None when
    there's no Django mount, or when the mount used ``clear_root_path`` (its
    patterns already embed the prefix, so Django's reverse is already correct).

    Returns the first Django mount found; ambiguous if several are configured!!!
    """
    for prefix, app in getattr(merged, "_asgi_mounts", []):
        if getattr(app, "_bolt_django_mount", False):
            if getattr(app, "_bolt_django_clear_root_path", False):
                return None
            return prefix.rstrip("/") or None
    return None


_runbolt = _Runbolt()
_merged = _runbolt.merge_apis(_runbolt.autodiscover_apis())
urlpatterns = _build_urlpatterns(_merged)
django_mount_prefix = _find_django_mount_prefix(_merged)
