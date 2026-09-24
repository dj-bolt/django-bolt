"""App under test for request lanes with django-tenants on PostgreSQL.

``TenantMainMiddleware`` sets the schema of the tenant on the database
connection of the thread that runs it. Bolt runs it on the lane of the
request, so every query of the request must use that lane: the user query of
``request.user``, ``request.auser()``, and ``CurrentUser``, and the ORM calls
of an async handler.

Each tenant has one user with the same primary key and the name
``<tenant>-user`` (see ``_tenants/management/commands/seed_tenants.py``). A
user loaded in the wrong schema thus has the wrong name. Requests select the
tenant with the ``Host`` header ``<tenant>.localhost``.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection

from django_bolt import BoltAPI, CurrentUser, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.concurrency import sync_to_thread

SECRET = "tenant-lanes-secret-key-longer-than-32-bytes"
TENANTS = ("alpha", "beta")

api = BoltAPI(django_middleware=["django_tenants.middleware.main.TenantMainMiddleware"])
auth = [JWTAuthentication(secret=SECRET)]


def _schema_name() -> str:
    return connection.schema_name


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/sync/me", auth=auth, guards=[IsAuthenticated()])
def sync_me(request: Request):
    return {"username": request.user.username, "schema": _schema_name()}


@api.get("/async/me", auth=auth, guards=[IsAuthenticated()])
async def async_me(request: Request):
    user = await request.auser()
    orm_user = await User.objects.aget()
    return {
        "username": user.username,
        "orm_username": orm_user.username,
        "schema": await sync_to_thread(_schema_name),
        "tenant": request.state["tenant"].schema_name,
    }


@api.get("/async/current", auth=auth, guards=[IsAuthenticated()])
async def async_current(user: CurrentUser):
    return {"username": user.username, "schema": await sync_to_thread(_schema_name)}
