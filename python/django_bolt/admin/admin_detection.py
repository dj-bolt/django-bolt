"""
Utilities for detecting and configuring Django admin integration.
"""

from __future__ import annotations

import contextlib
import logging
import sys

from django.conf import settings
from django.conf.urls.i18n import is_language_prefix_patterns_used
from django.contrib.sessions.middleware import SessionMiddleware
from django.urls import NoReverseMatch, get_resolver, get_script_prefix, reverse
from django.utils import translation
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
        ``"portal/staff"``. ``""`` when the admin site is at the root path,
        or when its URL pattern has a path parameter before it.
        ``"admin"`` when the URLconf has no admin site. ``None`` when the
        admin is not installed. For an admin site in ``i18n_patterns()``,
        this is the prefix of the default language.
    """
    prefixes = detect_admin_url_prefixes()
    return prefixes[0] if prefixes else None


def detect_admin_url_prefixes() -> list[str] | None:
    """
    Find each URL prefix of the Django admin site.

    An admin site in ``i18n_patterns()`` has one prefix for each language of
    ``settings.LANGUAGES``, for example ``"en/admin"`` and ``"fr/admin"``.
    The list then also has the prefix with no language, ``"admin"``. Django's
    ``LocaleMiddleware`` redirects that path to the prefix of a language.

    The default language is the variant of ``settings.LANGUAGE_CODE`` that
    ``settings.LANGUAGES`` has, as in ``LocaleMiddleware``. For ``"en-us"``
    with only ``"en"`` in ``LANGUAGES``, the first prefix is ``"en/admin"``.

    A path parameter before the admin site, as in
    ``path("<tenant>/admin/", site.urls)``, gives no static prefix. The list
    is then ``[""]``: Django gets each path that has no Bolt route.

    Returns:
        The prefixes with no slash at their ends. The prefix of the default
        language is first. ``None`` when the admin is not installed.
    """
    if not is_admin_installed():
        return None

    urlconf = getattr(settings, "ROOT_URLCONF", None)
    if not urlconf:
        return ["admin"]

    script_prefix = get_script_prefix()
    i18n_patterns_used, prefix_default_language = is_language_prefix_patterns_used(urlconf)
    # The default language is first. Without i18n_patterns(), each language has the same prefix.
    codes = [settings.LANGUAGE_CODE]
    if i18n_patterns_used:
        if prefix_default_language:
            # LANGUAGE_CODE can be a variant that LANGUAGES does not have, such as
            # "en-us" with only "en". Django then serves the default language at
            # the prefix of the variant that LANGUAGES has, and answers 404 at
            # the prefix of LANGUAGE_CODE.
            with contextlib.suppress(LookupError):
                codes = [translation.get_supported_language_variant(settings.LANGUAGE_CODE)]
        codes += [code for code, _name in settings.LANGUAGES]

    prefixes: list[str] = []
    for code in codes:
        with translation.override(code):
            try:
                index_url = reverse("admin:index", urlconf=urlconf)
            except NoReverseMatch:
                return [""] if _urlconf_has_admin_site(urlconf) else ["admin"]
            # reverse() puts the script prefix (FORCE_SCRIPT_NAME) first. A mount path does not have it.
            prefix = index_url.removeprefix(script_prefix).strip("/")
            language_prefix = f"{translation.get_language()}/"
        found = [prefix]
        if i18n_patterns_used and f"{prefix}/".startswith(language_prefix):
            found.append(prefix[len(language_prefix) :].strip("/"))
        prefixes.extend(item for item in found if item not in prefixes)
    return prefixes


def _urlconf_has_admin_site(urlconf: str) -> bool:
    """Tell if the URLconf has a URL pattern with the ``admin`` app namespace."""
    return "admin" in get_resolver(urlconf).app_dict


def admin_url_needs_path_parameter() -> bool:
    """
    Tell if the URL pattern of the admin site has a path parameter before it.

    An example is ``path("<tenant>/admin/", site.urls)``. Such a prefix is not
    a static path. Bolt thus mounts Django at the root path for it.
    """
    urlconf = getattr(settings, "ROOT_URLCONF", None)
    if not urlconf or not is_admin_installed():
        return False
    if not _urlconf_has_admin_site(urlconf):
        return False
    try:
        reverse("admin:index", urlconf=urlconf)
    except NoReverseMatch:
        return True
    return False


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
