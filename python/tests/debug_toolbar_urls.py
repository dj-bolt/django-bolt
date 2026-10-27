"""The URL patterns of django-debug-toolbar for a test that sets its own panels."""

from __future__ import annotations

from importlib import import_module

from django.urls import include, path


def toolbar_urlpatterns() -> list:
    """Return ``__debug__/`` URL patterns for the panels in the settings now.

    The toolbar builds its URL patterns one time, from the panels of that
    moment. ``debug_toolbar.urls`` keeps them in a module variable, and a
    change of ``DEBUG_TOOLBAR_PANELS`` does not rebuild them. Thus a test that
    sets other panels than an earlier test could not reverse its panel URLs.
    Call this function after the test sets ``DEBUG_TOOLBAR_PANELS``.
    """
    # The package attribute ``debug_toolbar.urls`` is a tuple, not the module.
    urls_module = import_module("debug_toolbar.urls")  # needs debug_toolbar in INSTALLED_APPS
    from debug_toolbar.toolbar import DebugToolbar  # noqa: PLC0415

    DebugToolbar._urlpatterns = None
    urls_module.urlpatterns = DebugToolbar.get_urls()
    return [path("__debug__/", include("debug_toolbar.urls"))]
