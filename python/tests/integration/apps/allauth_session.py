"""App under test: a django-allauth login, read by Bolt routes.

A user logs in through the login view of allauth, which Django serves under
``/accounts``, or through the headless API under ``/_allauth``. Bolt routes
then read the session user through Django's ``AuthenticationMiddleware`` and
allauth's ``AccountMiddleware``.

``AccountMiddleware`` is a sync-and-async factory: it picks its form from the
``get_response`` that it gets. ``DjangoMiddlewareStack`` gives it a sync
``get_response``, and a single ``DjangoMiddleware`` gives it an async one. So
the same routes are on two APIs: the stack at the root, and single wrappers
under ``/single``.

Each route family reads the user in each supported way: ``request.user`` in
a sync and an async handler, ``await request.auser()``, ``CurrentUser`` in a
sync and an async handler, and ``OptionalCurrentUser``. The
``/me/session-token`` routes read the user of an app session from the
``X-Session-Token`` header of allauth, through a dependency.

The routes under ``/token`` have no Django middleware. ``JWTAuthentication``
accepts the access tokens of the JWT strategy of allauth: with ``HS256``,
both use ``SECRET_KEY``. The routes under ``/token/session`` also reject a
token when its session ended, for example at logout.

Only a server project with allauth installed can import this module (see
``test_allauth_server_integration.py``).
"""

from __future__ import annotations

from typing import Annotated

from allauth.headless.internal.sessionkit import authenticate_by_x_session_token
from allauth.headless.tokens.strategies.jwt.internal import get_token_session, validate_token_user
from asgiref.sync import sync_to_async

from django_bolt import BoltAPI, CurrentUser, Depends, OptionalCurrentUser, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.concurrency import sync_to_thread
from django_bolt.exceptions import Unauthorized
from django_bolt.middleware import DjangoMiddleware
from django_bolt.param_functions import Header

DJANGO_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

api = BoltAPI(django_middleware=DJANGO_MIDDLEWARE)
single_api = BoltAPI(middleware=[DjangoMiddleware(path) for path in DJANGO_MIDDLEWARE])
token_api = BoltAPI()
JWT = [JWTAuthentication()]


def _token_session_is_valid(claims: dict) -> bool:
    session = get_token_session(claims)
    return session is not None and validate_token_user(claims, session) is not None


async def allauth_session_ended(jti: str, claims: dict) -> bool:
    """Reject an allauth access token when its session ended, for example at logout."""
    return not await sync_to_thread(_token_session_is_valid, claims)


SESSION_BOUND_JWT = [JWTAuthentication(revoked_token_handler=allauth_session_ended)]
AUTHENTICATED = [IsAuthenticated()]


def _describe(user) -> dict:
    return {"authenticated": bool(user.is_authenticated), "username": user.get_username() or None}


def _session_token_user(found: tuple | None):
    if found is None:
        raise Unauthorized(detail="A valid X-Session-Token header is necessary.")
    return found[0]


SessionToken = Annotated[str | None, Header(alias="X-Session-Token")]


def session_token_user(token: SessionToken = None):
    """The user of an allauth app session. The ORM query runs in the thread of a sync handler."""
    return _session_token_user(authenticate_by_x_session_token(token) if token else None)


async def asession_token_user(token: SessionToken = None):
    """The async form, for an async handler. Behind the stack, ``sync_to_async`` runs the query on the lane."""
    found = await sync_to_async(authenticate_by_x_session_token)(token) if token else None
    return _session_token_user(found)


def _register(target: BoltAPI) -> None:
    @target.get("/me/sync")
    def me_sync(request: Request):
        return {**_describe(request.user), "allauth": "allauth" in request.state}

    @target.get("/me/async")
    async def me_async(request: Request):
        # A sync read of the session user runs its query on the lane of the request.
        return {**_describe(request.user), "allauth": "allauth" in request.state}

    @target.get("/me/auser")
    async def me_auser(request: Request):
        return _describe(await request.auser())

    @target.get("/me/current")
    async def me_current(user: CurrentUser):
        return _describe(user)

    @target.get("/me/current-sync")
    def me_current_sync(user: CurrentUser):
        return _describe(user)

    @target.get("/me/optional")
    async def me_optional(user: OptionalCurrentUser):
        return {"username": None if user is None else user.get_username()}

    @target.get("/me/session-token")
    async def me_session_token(user=Depends(asession_token_user)):
        return _describe(user)

    @target.get("/me/session-token-sync")
    def me_session_token_sync(user=Depends(session_token_user)):
        return _describe(user)


@token_api.get("/me/sync", auth=JWT, guards=AUTHENTICATED)
def token_me_sync(request: Request):
    return _describe(request.user)


@token_api.get("/me/async", auth=JWT, guards=AUTHENTICATED)
async def token_me_async(request: Request):
    return _describe(request.user)


@token_api.get("/me/current", auth=JWT, guards=AUTHENTICATED)
async def token_me_current(user: CurrentUser):
    return _describe(user)


@token_api.get("/me/current-sync", auth=JWT, guards=AUTHENTICATED)
def token_me_current_sync(user: CurrentUser):
    return _describe(user)


@token_api.get("/session/me/sync", auth=SESSION_BOUND_JWT, guards=AUTHENTICATED)
def token_session_me_sync(request: Request):
    return _describe(request.user)


@token_api.get("/session/me/current", auth=SESSION_BOUND_JWT, guards=AUTHENTICATED)
async def token_session_me_current(user: CurrentUser):
    return _describe(user)


@api.get("/health")
async def health():
    return {"status": "ok"}


_register(api)
_register(single_api)
api.mount("/single", single_api)
api.mount("/token", token_api)
api.mount_django("/accounts", clear_root_path=True)
api.mount_django("/_allauth", clear_root_path=True)
