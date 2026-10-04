"""Docs for staff users who log in to the Django admin (issue #378).

The project default is JWT auth with ``IsAuthenticated``, as in the issue. A
browser sends no token, so the docs set ``SessionAuthentication``. ``runbolt``
serves the Django admin login under ``/admin``. Django's ``LoginView`` under
``/accounts`` logs in a user who is not staff. ``/me`` is a plain route with
the same backend.
"""

from __future__ import annotations

from django_bolt import BoltAPI, CurrentUser
from django_bolt.auth import AllowAny, IsAuthenticated, Requires, SessionAuthentication
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Staff docs",
        version="1",
        auth=[SessionAuthentication(login_url="/admin/login/")],
        guards=[Requires("is_staff", True)],
    )
)


@api.get("/health", guards=[AllowAny()])
async def health():
    return {"ok": True}


@api.get("/me", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
def me(user: CurrentUser):
    return {"username": user.username}


# Django's LoginView, for a user who is not staff. runbolt mounts /admin by itself.
api.mount_django("/accounts", clear_root_path=True)
