"""
Django middleware loader for Django-Bolt.

Automatically loads middleware from Django's settings.MIDDLEWARE configuration,
providing seamless integration with existing Django projects.

Usage:
    # Use all Django middleware from settings.MIDDLEWARE
    api = BoltAPI(django_middleware=True)

    # Exclude specific middleware
    api = BoltAPI(django_middleware={"exclude": ["django.middleware.csrf.CsrfViewMiddleware"]})

    # Only include specific middleware
    api = BoltAPI(django_middleware={"include": [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ]})
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .django_adapter import DjangoMiddlewareStack

if TYPE_CHECKING:
    from .middleware import MiddlewareType


# Middleware that may be excluded for API-only endpoints (opt-in exclusion)
# By default, ALL middleware from settings.MIDDLEWARE are loaded
DEFAULT_EXCLUDED_MIDDLEWARE: set = set()  # Empty by default - load everything


def _dotted_paths(value: Any) -> list[str]:
    """Check that `value` is a collection of dotted path strings. Raise if it is not."""
    if isinstance(value, str) or not all(isinstance(path, str) for path in value):
        raise ImproperlyConfigured(
            f"django_middleware include/exclude entries must be a list of dotted path strings, got {value!r}."
        )
    return list(value)


def load_django_middleware(
    config: bool | list[str] | dict[str, Any] = True,
    *,
    exclude_defaults: bool = True,
) -> list[MiddlewareType]:
    """
    Load Django middleware for Bolt routes.

    Args:
        config: Middleware configuration. Can be:
            - True: Load all middleware from settings.MIDDLEWARE
            - False/None: Return empty list
            - List[str]: Load exactly these dotted paths, in this order.
              They do not need to be in settings.MIDDLEWARE.
            - Dict that filters settings.MIDDLEWARE:
                - "include": keep only these paths
                - "exclude": drop these paths
        exclude_defaults: If True, also drop DEFAULT_EXCLUDED_MIDDLEWARE.

    Returns:
        A list with one DjangoMiddlewareStack, or an empty list.

    Raises:
        ImproperlyConfigured: An entry is not a dotted path string.
        ImportError: A dotted path does not import.

    Like Django's own `load_middleware`, errors stop startup. Nothing is skipped
    without a message.
    """
    if config is False or config is None:
        return []

    if isinstance(config, list):
        paths = config
    else:
        exclude_set: set[str] = set(DEFAULT_EXCLUDED_MIDDLEWARE) if exclude_defaults else set()
        include_set: set[str] | None = None
        if isinstance(config, dict):
            if "include" in config:
                include_set = set(_dotted_paths(config["include"]))
            if "exclude" in config:
                exclude_set.update(_dotted_paths(config["exclude"]))
        paths = [
            path
            for path in getattr(settings, "MIDDLEWARE", [])
            if (include_set is None or path in include_set) and path not in exclude_set
        ]

    middleware_classes: list = []
    for path in paths:
        if not isinstance(path, str):
            raise ImproperlyConfigured(
                f"django_middleware entries must be dotted path strings, got {path!r}. "
                "To use a middleware class directly, pass "
                "BoltAPI(middleware=[DjangoMiddlewareStack([...])])."
            )
        middleware_classes.append(import_string(path))

    # One DjangoMiddlewareStack for all middleware: one Bolt->Django request
    # conversion at the start and one Django->Bolt response conversion at the end.
    if middleware_classes:
        return [DjangoMiddlewareStack(middleware_classes)]
    return []


def get_django_middleware_setting() -> list[str]:
    """
    Get the current MIDDLEWARE setting from Django.

    Returns:
        List of middleware class paths from settings.MIDDLEWARE
    """
    try:
        return list(getattr(settings, "MIDDLEWARE", []))
    except Exception:
        return []


__all__ = [
    "load_django_middleware",
    "get_django_middleware_setting",
    "DEFAULT_EXCLUDED_MIDDLEWARE",
]
