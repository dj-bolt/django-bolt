"""App under test for recovery from a database connection that died.

``/me`` is the path that failed in production: an async handler behind the
default ``JWTAuthentication`` reads ``request.user``. The lazy load runs
``get_user_sync`` on the bounded ORM pool through ``run_orm_blocking``, so the
Django connection it uses is parked on a pool thread between requests. When
the database server closes that connection, the next load must fail once at
most, and the request after it must reconnect.

``/seed`` migrates the database of the subprocess and creates the test user,
since server projects start with an unmigrated database. ``SECRET`` and
``USERNAME`` are exported so tests can mint matching tokens.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management import call_command

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

SECRET = "dead-connection-recovery-secret-key-32-bytes"
USERNAME = "recovered"

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/seed")
def seed():
    call_command("migrate", interactive=False, verbosity=0)
    user, _ = User.objects.get_or_create(username=USERNAME)
    return {"user_id": user.id}


@api.get("/me", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
async def me(request):
    # Forces the lazy load: load_via_sync -> run_orm_blocking -> get_user_sync.
    return {"username": request.user.username}
