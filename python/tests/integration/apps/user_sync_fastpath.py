"""App under test for ``request.user`` on Rust's sync-dispatch fast path.

``/me-trivial/{item_id}`` is an ``async def`` handler with no await, no
middleware, and JWT auth. In a real server it takes the ``can_sync_dispatch``
branch, and Rust installs the WorkerLoop as the running loop around
``_dispatch_sync``. The user loader must then send its query to the ORM pool.
It reads ``request.user`` in a helper: a read in the source of the handler
makes Bolt load the user first, which leaves the fast path.
``/me-sync/{item_id}`` is the plain ``def`` variant, which runs with no
running loop. ``TestClient`` never takes the sync-dispatch branch, so only a
``runbolt`` subprocess covers this path. ``get_user_sync`` runs a raw query,
so the app needs no tables.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from django.db import connection

from django_bolt import BoltAPI, Request
from django_bolt.auth import JWTAuthentication

SECRET = "user-sync-fastpath-secret-longer-than-32-characters"


class DatabaseAuth(JWTAuthentication):
    def get_user_sync(self, user_id):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return SimpleNamespace(username="bob", loaded_on=threading.current_thread().name)


api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


def _user_of(request: Request):
    return request.user


@api.get("/me-trivial/{item_id}", auth=[DatabaseAuth(secret=SECRET)])
async def me_trivial(item_id: int, request: Request):
    user = _user_of(request)
    return {"username": user.username, "loaded_on": user.loaded_on, "handler_on": threading.current_thread().name}


@api.get("/me-sync/{item_id}", auth=[DatabaseAuth(secret=SECRET)])
def me_sync(item_id: int, request: Request):
    user = request.user
    return {"username": user.username, "loaded_on": user.loaded_on, "handler_on": threading.current_thread().name}
