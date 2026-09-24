"""App under test: a django-allauth session login, read by Bolt routes.

A user logs in through the login view of allauth, which Django serves under
``/accounts``. Bolt routes then read the session user through Django's
``AuthenticationMiddleware`` and allauth's ``AccountMiddleware``.

``AccountMiddleware`` is a sync-and-async factory: it picks its form from the
``get_response`` that it gets. ``DjangoMiddlewareStack`` gives it a sync
``get_response``, and a single ``DjangoMiddleware`` gives it an async one. So
the same routes are on two APIs: the stack at the root, and single wrappers
under ``/single``.

Each route family reads the user in each supported way: ``request.user`` in
a sync and an async handler, ``await request.auser()``, ``CurrentUser`` in a
sync and an async handler, and ``OptionalCurrentUser``.

Only a server project with allauth installed can import this module (see
``test_allauth_server_integration.py``).
"""

from __future__ import annotations

from django_bolt import BoltAPI, CurrentUser, OptionalCurrentUser, Request
from django_bolt.middleware import DjangoMiddleware

DJANGO_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

api = BoltAPI(django_middleware=DJANGO_MIDDLEWARE)
single_api = BoltAPI(middleware=[DjangoMiddleware(path) for path in DJANGO_MIDDLEWARE])


def _describe(user) -> dict:
    return {"authenticated": bool(user.is_authenticated), "username": user.get_username() or None}


def _register(target: BoltAPI) -> None:
    @target.get("/me/sync")
    def me_sync(request: Request):
        return {**_describe(request.user), "allauth": "allauth" in request.state}

    @target.get("/me/async")
    async def me_async(request: Request):
        # Bolt loads the session user before the handler, because the route has AuthenticationMiddleware.
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


@api.get("/health")
async def health():
    return {"status": "ok"}


_register(api)
_register(single_api)
api.mount("/single", single_api)
api.mount_django("/accounts", clear_root_path=True)
