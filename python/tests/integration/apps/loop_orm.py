"""App under test for async ORM reads on the event loop of a real server.

``TestClient`` runs a handler on a worker thread of its own. A real server
runs it on an Actix worker thread, which has its own Tokio runtime. The loop
connections of that thread send their queries from that runtime.

``/seed`` migrates the database of the subprocess and creates the users.

``/usernames`` reads with ``async for``, and ``/user/{username}`` reads with
``aget()``.

``/backends/{count}`` runs ``count`` reads at one time and returns the number
of database connections that ran them. A read on the ORM thread pool uses the
connection of its thread, so one ORM thread gives one connection.
"""

from __future__ import annotations

import asyncio

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models.expressions import RawSQL

from django_bolt import BoltAPI

USERNAMES = ["ada", "bob", "cy"]

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/seed")
def seed():
    call_command("migrate", interactive=False, verbosity=0)
    for username in USERNAMES:
        User.objects.get_or_create(username=username)
    return {"users": User.objects.count()}


@api.get("/usernames")
async def usernames():
    return [name async for name in User.objects.order_by("username").values_list("username", flat=True)]


@api.get("/user/{username}")
async def user(username: str):
    found = await User.objects.aget(username=username)
    return {"username": found.username, "is_active": found.is_active}


async def _backend() -> int:
    slow = User.objects.annotate(pid=RawSQL("pg_backend_pid()", []), nap=RawSQL("(SELECT 1 FROM pg_sleep(0.05))", []))
    return (await slow.aget(username=USERNAMES[0])).pid


@api.get("/backends/{count}")
async def backends(count: int):
    return {"connections": len(set(await asyncio.gather(*(_backend() for _ in range(count)))))}
