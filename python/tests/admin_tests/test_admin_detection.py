"""Tests for admin dependency detection (``should_enable_admin``).

Admin is auto-mounted via Django's own ASGI application, whose request handling
runs ``settings.MIDDLEWARE``. Admin's real session requirement is therefore a
``SessionMiddleware`` in that chain (mirroring Django's own ``admin.E410``
system check) — NOT the ``django.contrib.sessions`` app in ``INSTALLED_APPS``.
This lets alternative backends such as django-qsessions work.
"""

from __future__ import annotations

from django.conf import settings

from django_bolt.admin.admin_detection import should_enable_admin

QSESSIONS_MIDDLEWARE = "tests.admin_tests.fake_qsessions.SessionMiddleware"
DJANGO_SESSION_MIDDLEWARE = "django.contrib.sessions.middleware.SessionMiddleware"


def _installed_without_sessions() -> list[str]:
    return [app for app in settings.INSTALLED_APPS if app != "django.contrib.sessions"]


def test_admin_enabled_with_qsessions_style_backend(monkeypatch):
    """Admin enables when the sessions app is replaced by a subclassed backend.

    Simulates a django-qsessions install: ``django.contrib.sessions`` is dropped
    from INSTALLED_APPS and its middleware is swapped for a subclass. Admin must
    still be recognized as usable.
    """
    middleware = [QSESSIONS_MIDDLEWARE if m == DJANGO_SESSION_MIDDLEWARE else m for m in settings.MIDDLEWARE]
    monkeypatch.setattr(settings, "INSTALLED_APPS", _installed_without_sessions())
    monkeypatch.setattr(settings, "MIDDLEWARE", middleware)

    assert should_enable_admin() is True


def test_admin_enabled_with_stock_sessions_app_removed(monkeypatch):
    """The stock middleware still counts even when the sessions app is absent."""
    monkeypatch.setattr(settings, "INSTALLED_APPS", _installed_without_sessions())
    monkeypatch.setattr(settings, "MIDDLEWARE", list(settings.MIDDLEWARE))

    assert should_enable_admin() is True


def test_admin_disabled_without_any_session_middleware(monkeypatch):
    """Admin stays disabled when no SessionMiddleware (or subclass) is present."""
    middleware = [m for m in settings.MIDDLEWARE if m != DJANGO_SESSION_MIDDLEWARE]
    monkeypatch.setattr(settings, "INSTALLED_APPS", _installed_without_sessions())
    monkeypatch.setattr(settings, "MIDDLEWARE", middleware)

    assert should_enable_admin() is False


def test_admin_still_enabled_with_default_settings():
    """Regression guard: the stock test project (sessions app + middleware) works."""
    assert should_enable_admin() is True
