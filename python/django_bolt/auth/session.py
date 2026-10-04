"""Django session authentication for Bolt routes.

Django does the session work. This module adds Django's session and auth
middleware to a route that needs them, and checks the guards of the route
against the user that Django loads. Bolt runs both at registration and
reuses the result for each request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .._core import GuardCheck
from ..concurrency import in_lane_mode
from ..middleware.django_adapter import DjangoMiddleware, DjangoMiddlewareStack
from ..middleware_response import MiddlewareResponse
from .backends import SessionAuthentication, get_default_authentication_classes
from .guards import QUANT_NONE

# Django's auth middleware imports the auth models, so Bolt imports these at
# registration, when the app registry is ready.
_SESSION_MIDDLEWARE = "django.contrib.sessions.middleware.SessionMiddleware"
_AUTH_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"

# The claims that a session user has. Other claims are absent.
_SESSION_CLAIMS = frozenset({"sub", "is_staff", "is_superuser", "permissions"})
_NO_PERMISSIONS: frozenset[str] = frozenset()

SessionGuardCheck = Callable[[Any], Awaitable[MiddlewareResponse | None]]


def uses_session(backends: Sequence[Any]) -> bool:
    """Whether a session backend is in the auth backends of a route."""
    return any(isinstance(backend, SessionAuthentication) for backend in backends)


def auth_without_session(auth: list[Any] | None, route: str) -> list[Any] | None:
    """The auth of a route that runs no Django middleware: a WebSocket route or an MCP mount.

    An explicit session backend is an error. A session backend from
    ``BOLT_AUTHENTICATION_CLASSES`` cannot authenticate there, so Bolt
    leaves it out. Returns None when the route uses the defaults as they are.
    """
    if auth is not None:
        if uses_session(auth):
            raise ImproperlyConfigured(
                f"SessionAuthentication works on HTTP routes only. {route} cannot use it. Remove it from auth=[...]."
            )
        return auth
    defaults = get_default_authentication_classes()
    if not uses_session(defaults):
        return None
    return [backend for backend in defaults if not isinstance(backend, SessionAuthentication)]


def _django_middleware_classes(spec: Any) -> list[Any]:
    if isinstance(spec, DjangoMiddlewareStack):
        return list(spec.middleware_classes)
    if isinstance(spec, DjangoMiddleware):
        return [spec.middleware_class]
    return []


def session_middleware(specs: Sequence[Any], route: str) -> list[DjangoMiddlewareStack]:
    """The Django middleware that a session route needs and does not run yet.

    ``specs`` is all the middleware of the route: the API, the router and the
    route. The result is empty, or one stack to put before the router middleware.
    """
    session_class, auth_class = import_string(_SESSION_MIDDLEWARE), import_string(_AUTH_MIDDLEWARE)
    present = [cls for spec in specs for cls in _django_middleware_classes(spec) if isinstance(cls, type)]
    has_session = any(issubclass(cls, session_class) for cls in present)
    has_auth = any(issubclass(cls, auth_class) for cls in present)
    if has_session and has_auth:
        return []
    if not has_session and not has_auth:
        return [DjangoMiddlewareStack([session_class, auth_class])]
    # A second stack would load and save the session a second time.
    have, need = (
        ("SessionMiddleware", "AuthenticationMiddleware")
        if has_session
        else ("AuthenticationMiddleware", "SessionMiddleware")
    )
    raise ImproperlyConfigured(
        f"SessionAuthentication on {route} needs Django's SessionMiddleware and AuthenticationMiddleware. "
        f"The middleware of the route has {have} but not {need}. Add {need} to the same middleware list."
    )


def build_session_guard_check(
    guards_metadata: list[dict[str, Any]] | None,
    backends: Sequence[Any],
    route: str,
) -> SessionGuardCheck | None:
    """Build the guard check for the session requests of a route.

    Rust cannot read a Django session, so it does not check the guards of a
    session request. This check runs after Django's middleware loaded the
    user, before the handler. It uses the guard evaluator of Rust.

    Returns None when the route has no session backend or no guard that can
    reject a request.
    """
    if not uses_session(backends) or not guards_metadata:
        return None
    if all(guard["type"] == "allow_any" for guard in guards_metadata):
        return None

    claims = {guard["claim"] for guard in guards_metadata if guard["type"] == "requires"}
    for guard in guards_metadata:
        if guard["type"] == "requires" and guard["quantifier"] == QUANT_NONE and guard["claim"] not in _SESSION_CLAIMS:
            raise ImproperlyConfigured(
                f"Requires({guard['claim']!r}, none_of=...) on {route} lets every session user in, "
                f"because a session user has no {guard['claim']!r} claim. A session user has "
                f"{', '.join(sorted(_SESSION_CLAIMS))}."
            )

    # Read only what a guard reads. Check at registration that the user model has it.
    reads_staff = "is_staff" in claims
    reads_superuser = "is_superuser" in claims
    reads_permissions = "permissions" in claims
    user_model = get_user_model()
    for reads, attribute in (
        (reads_staff, "is_staff"),
        (reads_superuser, "is_superuser"),
        (reads_permissions, "aget_all_permissions"),
    ):
        if reads and not hasattr(user_model, attribute):
            raise ImproperlyConfigured(
                f"A guard on {route} reads {attribute.removeprefix('aget_all_')} of the session user, "
                f"but the user model {user_model.__name__} has no {attribute}."
            )

    from django.contrib.auth.views import redirect_to_login  # noqa: PLC0415 - imports the auth models

    guard_check = GuardCheck(guards_metadata)
    # By the position that Rust reports as auth_backend_index.
    login_urls = tuple(
        backend.login_url if isinstance(backend, SessionAuthentication) else None for backend in backends
    )

    def verdict(request: Any) -> tuple[int, bytes] | None:
        # Reading request.user runs Django's get_user: the session and the user queries.
        user = request.user
        if not user.is_authenticated:
            return guard_check.check(None, False, False, _NO_PERMISSIONS)
        return guard_check.check(
            str(user.pk),
            user.is_staff if reads_staff else False,
            user.is_superuser if reads_superuser else False,
            user.get_all_permissions() if reads_permissions else _NO_PERMISSIONS,
        )

    # One hop runs the whole check on the thread of the request, as asgiref
    # picks it for Django's ORM. Django's async auth chain takes one hop per query.
    averdict = sync_to_async(verdict, thread_sensitive=True)

    async def check_session_guards(request: Any) -> MiddlewareResponse | None:
        context = request.context
        if context is None or context["auth_backend"] != "session":
            # Rust checked the guards for a token or for no credential.
            return None

        # A lane runs the whole request on its thread with no suspend.
        rejection = verdict(request) if in_lane_mode() else await averdict(request)
        # request.user is loaded. Without Django's auser, request.auser() gives it with no query.
        request.state.pop("auser", None)
        if rejection is None:
            return None
        status_code, body = rejection
        login_url = login_urls[context["auth_backend_index"]]
        if status_code == 401 and login_url is not None:
            redirect = redirect_to_login(request.get_full_path(), login_url)
            return MiddlewareResponse(302, {"location": redirect["Location"]}, b"", "empty")
        return MiddlewareResponse(status_code, {}, body)

    return check_session_guards
