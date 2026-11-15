"""
Utilities for detecting and configuring Django admin integration.
"""

from __future__ import annotations

import logging
import sys

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.urls import NoReverseMatch, get_script_prefix, reverse
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


def _has_session_middleware() -> bool:
    """Check whether a session middleware is configured in ``settings.MIDDLEWARE``.

    Django admin is auto-mounted through Django's own ASGI application, whose
    request handling runs ``settings.MIDDLEWARE``. Admin therefore needs a
    ``SessionMiddleware`` in that chain to maintain login state — it does *not*
    require the ``django.contrib.sessions`` app to be in ``INSTALLED_APPS``.

    Matching against a subclass (rather than the exact class) mirrors Django's
    own admin system check (``admin.E410``), so alternative session backends
    whose middleware subclasses the stock ``SessionMiddleware`` — e.g.
    django-qsessions' ``qsessions.middleware.SessionMiddleware`` — are recognized.

    Returns:
        True if a ``SessionMiddleware`` (or subclass) is present, else False.
    """
    for middleware_path in getattr(settings, "MIDDLEWARE", None) or ():
        try:
            middleware_class = import_string(middleware_path)
        except ImportError:
            # Unimportable middleware is reported elsewhere; skip it here.
            continue
        if isinstance(middleware_class, type) and issubclass(middleware_class, SessionMiddleware):
            return True

    return False


def is_admin_installed() -> bool:
    """
    Check if Django admin is installed and configured.

    Returns:
        True if admin is in INSTALLED_APPS, False otherwise
    """
    try:
        return "django.contrib.admin" in settings.INSTALLED_APPS
    except Exception:
        return False


def detect_admin_url_prefix() -> str | None:
    """
    Find the URL prefix of the Django admin site.

    Django resolves the admin index through the URL resolver tree. Thus the
    prefix is correct for a regex pattern, a nested ``include()`` and an
    ``AdminSite`` with its own name.

    Returns:
        The prefix with no slash at its ends, for example ``"admin"`` or
        ``"portal/staff"``. ``""`` when the admin site is at the root path.
        ``"admin"`` when the URLconf has no admin site that Django can
        reverse. ``None`` when the admin is not installed.
    """
    if not is_admin_installed():
        return None

    urlconf = getattr(settings, "ROOT_URLCONF", None)
    if not urlconf:
        return "admin"

    try:
        index_url = reverse("admin:index", urlconf=urlconf)
    except NoReverseMatch:
        return "admin"

    # reverse() puts the script prefix (FORCE_SCRIPT_NAME) first. A mount path does not have it.
    return index_url.removeprefix(get_script_prefix()).strip("/")


def get_admin_route_patterns() -> list[tuple[str, list[str]]]:
    """
    Get route patterns to register for Django admin.

    Returns:
        List of (path_pattern, methods) tuples for admin routes
        Example: [('/admin/{path:path}', ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])]
    """
    if not is_admin_installed():
        return []

    # Detect admin URL prefix
    admin_prefix = detect_admin_url_prefix()
    if not admin_prefix:
        return []

    # Build catch-all pattern for admin routes
    # Use {path:path} syntax for catch-all parameter
    admin_pattern = f"/{admin_prefix}/{{path:path}}"

    # Admin needs to handle common HTTP methods
    # Only use methods supported by django-bolt's router (GET, POST, PUT, PATCH, DELETE)
    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]

    # Also add exact /admin route (without trailing slash)
    # NOTE: NormalizePath::trim() in Rust strips trailing slashes from incoming requests,
    # so we register routes WITHOUT trailing slashes to match the normalized paths.
    # However, for TestClient with use_http_layer=True, we also need the trailing slash version.
    admin_root = f"/{admin_prefix}"
    admin_root_slash = f"/{admin_prefix}/"

    return [
        (admin_root, methods),
        (admin_root_slash, methods),
        (admin_pattern, methods),
    ]


def get_static_url_prefix() -> str | None:
    """
    Get the STATIC_URL prefix from Django settings.

    Returns:
        Static URL prefix (e.g., 'static') or None if not configured
    """
    try:
        if hasattr(settings, "STATIC_URL") and settings.STATIC_URL:
            static_url = settings.STATIC_URL
            # Remove leading/trailing slashes
            return static_url.strip("/")
    except Exception as e:
        logger.warning(
            "Failed to get STATIC_URL from Django settings. Static file serving may not work correctly. Error: %s", e
        )

    return None


def should_enable_admin() -> bool:
    """
    Determine if admin should be auto-enabled.

    Returns:
        True if admin is installed and can be enabled, False otherwise
    """
    if not is_admin_installed():
        return False

    # Check if required dependencies are installed
    try:
        # These apps must be in INSTALLED_APPS for admin to function.
        # NOTE: django.contrib.sessions is intentionally NOT required here —
        # admin only needs a SessionMiddleware in the chain (checked below),
        # which alternative backends like django-qsessions provide via a
        # subclass while replacing the sessions app in INSTALLED_APPS.
        required_apps = [
            "django.contrib.auth",
            "django.contrib.contenttypes",
        ]

        for app in required_apps:
            if app not in settings.INSTALLED_APPS:
                print(
                    f"[django-bolt] Warning: Django admin is installed but {app} is missing. "
                    f"Admin integration disabled.",
                    file=sys.stderr,
                )
                return False

        # Admin needs session support in the middleware chain to track login
        # state. Mirrors Django's own admin.E410 system check.
        if not _has_session_middleware():
            print(
                "[django-bolt] Warning: Django admin is installed but no SessionMiddleware "
                "(django.contrib.sessions.middleware.SessionMiddleware or a subclass, e.g. "
                "qsessions.middleware.SessionMiddleware) was found in settings.MIDDLEWARE. "
                "Admin integration disabled.",
                file=sys.stderr,
            )
            return False

        return True

    except Exception as e:
        print(f"[django-bolt] Warning: Could not check admin dependencies: {e}", file=sys.stderr)
        return False


def get_admin_info() -> dict:
    """
    Get information about Django admin configuration.

    Returns:
        Dict with admin configuration details
    """
    return {
        "installed": is_admin_installed(),
        "enabled": should_enable_admin(),
        "url_prefix": detect_admin_url_prefix(),
        "static_url": get_static_url_prefix(),
    }
