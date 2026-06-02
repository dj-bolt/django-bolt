"""URL reversing for Django-Bolt routes.

Bolt routes live in the Rust matchit router, not in Django's ``ROOT_URLCONF``, so
Django's native ``reverse()`` can't see them. A synthetic urlconf built from the
merged Bolt routes lives in ``django_bolt._bolt_urlconf`` (referenced here by
name); Django's own resolver reverses against it, giving us converters,
namespaces, ``args``/``kwargs``, and ``query``/``fragment`` for free.

``reverse``/``reverse_lazy`` are the public, explicit entry points
(``from django_bolt.urls import reverse``) and are also what the optional
monkeypatch installs over ``django.urls.reverse`` so ``{% url %}`` resolves Bolt
routes. When invoked that way ``urlconf`` is ``None``; we try the Bolt urlconf
first and fall through to the original Django reverse (``ROOT_URLCONF``) on miss.
"""

from __future__ import annotations

import importlib
import sys

import django.urls
import django.urls.base
from django.urls import reverse as _django_reverse
from django.urls.exceptions import NoReverseMatch
from django.utils.functional import lazy

# Importable name of the synthetic urlconf module. Passing the name (not a fresh
# module object) lets get_resolver's lru_cache key on it and reuse the resolver.
_BOLT_URLCONF = "django_bolt._bolt_urlconf"

# Prefix a mounted Django app is served under, resolved once on first use (it's a
# build-time constant on _bolt_urlconf). Sentinel distinguishes "not resolved yet"
# from the resolved value ("" when there's no Django mount).
_UNRESOLVED = object()
_django_mount_prefix = _UNRESOLVED
_has_bolt_routes = _UNRESOLVED


def reverse(
    viewname,
    urlconf=None,
    args=None,
    kwargs=None,
    current_app=None,
    *,
    query=None,
    fragment=None,
):
    """Reverse a Bolt (or Django) route name to a URL.

    Drop-in replacement for ``django.urls.reverse``. When ``urlconf`` is ``None``
    (the case for ``{% url %}`` and direct calls), the Bolt urlconf is tried
    first; on ``NoReverseMatch`` we fall through to the original Django reverse so
    plain Django names keep working unchanged.

    Differs from Django: Bolt paths are untyped (matchit ``{x}``), so params are
    not converter-validated - a mismatch surfaces as ``NoReverseMatch``.

    Short-circuits to plain Django when there are no Bolt routes, so the global
    patch is a transparent no-op for projects (or tests) without a Bolt API.
    """
    if urlconf is None and _bolt_routes_present():
        try:
            return _django_reverse(
                viewname, _BOLT_URLCONF, args, kwargs, current_app, query=query, fragment=fragment
            )
        except NoReverseMatch:
            pass  # not a Bolt route - fall through to Django's ROOT_URLCONF
        # Django's ROOT_URLCONF is served under the Django mount prefix in a Bolt
        # process, so prepend it. A miss raises NoReverseMatch from here as usual.
        return _mount_prefix() + _django_reverse(
            viewname, None, args, kwargs, current_app, query=query, fragment=fragment
        )
    return _django_reverse(
        viewname, urlconf, args, kwargs, current_app, query=query, fragment=fragment
    )


reverse_lazy = lazy(reverse, str)


def _bolt_routes_present() -> bool:
    """Whether the synthetic urlconf has any Bolt routes (cached, built on first use)."""
    global _has_bolt_routes
    if _has_bolt_routes is _UNRESOLVED:
        _has_bolt_routes = bool(importlib.import_module(_BOLT_URLCONF).urlpatterns)
    return _has_bolt_routes


def _mount_prefix() -> str:
    """Cached prefix the mounted Django app is served under ("" if none)."""
    global _django_mount_prefix
    if _django_mount_prefix is _UNRESOLVED:
        _django_mount_prefix = (
            getattr(importlib.import_module(_BOLT_URLCONF), "django_mount_prefix", None) or ""
        )
    return _django_mount_prefix


def patch_django_reverse() -> None:
    """Install ``reverse``/``reverse_lazy`` over Django's globally.

    Idempotent. Making this truly global takes three steps, because callers bind
    ``reverse`` in different ways:

    1. Repoint the canonical definition (``django.urls.base``) and the package
       re-export (``django.urls``). The render-time ``from django.urls import
       reverse`` inside the ``{% url %}`` tag reads the re-export, so templates
       are covered by this alone.
    2. Replace ``reverse_lazy`` too — Django's is ``lazy(reverse, str)`` frozen
       around the *original* reverse, so patching reverse doesn't reach it.
    3. Sweep ``sys.modules`` for modules that did ``from django.urls import
       reverse``/``reverse_lazy`` at import time and froze a local copy (e.g.
       ``django.shortcuts``). Modules imported *after* this runs pick up the
       patched names naturally.

    Our ``reverse`` keeps a reference to the original via the module-level
    ``_django_reverse`` (captured at import, before this patch), so the
    Bolt-miss fall-through still reaches Django — no recursion.
    """
    if getattr(django.urls.base.reverse, "_bolt_patched", False):
        return  # already installed (idempotent; guards against re-capture)

    original_reverse = django.urls.base.reverse
    original_reverse_lazy = django.urls.base.reverse_lazy
    reverse._bolt_patched = True

    # 1 + 2: canonical definition, package re-export, and reverse_lazy.
    django.urls.base.reverse = reverse
    django.urls.reverse = reverse
    django.urls.base.reverse_lazy = reverse_lazy
    django.urls.reverse_lazy = reverse_lazy

    # 3: repoint consumers that froze the names locally before the patch.
    for module in list(sys.modules.values()):
        mod_name = getattr(module, "__name__", "") or ""
        if module is None or mod_name.startswith(("django.urls", "django_bolt.urls")):
            continue  # skip the source modules and our own (preserves _django_reverse)
        if getattr(module, "reverse", None) is original_reverse:
            module.reverse = reverse
        if getattr(module, "reverse_lazy", None) is original_reverse_lazy:
            module.reverse_lazy = reverse_lazy


__all__ = ["reverse", "reverse_lazy", "patch_django_reverse"]
