"""App under test for cookie-based JWT auth and custom user loading.

Defines routes protected by ``JWTAuthentication(cookie=...)`` so a real
``runbolt`` server proves the cookie config flows from backend metadata into
Rust extraction, plus a route whose backend overrides ``get_user`` to resolve
``request.user`` by username instead of the default pk lookup. ``/seed``
migrates the subprocess's sqlite database and creates the test user, since
server projects start with an unmigrated DB. ``SECRET`` and ``USERNAME`` are
exported so tests can mint matching tokens.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management import call_command

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

SECRET = "jwt-cookie-server-integration-secret"
USERNAME = "cookieuser"


class UsernameJWT(JWTAuthentication):
    """Resolves request.user by username — the default pk lookup can never
    satisfy these tokens, so a successful load proves the override ran."""

    async def get_user(self, user_id, auth_context):
        return await User.objects.aget(username=user_id)


api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/seed")
def seed():
    call_command("migrate", interactive=False, verbosity=0)
    user, _ = User.objects.get_or_create(username=USERNAME)
    return {"user_id": user.id, "username": user.username}


@api.get(
    "/whoami",
    auth=[JWTAuthentication(secret=SECRET, cookie="access_token")],
    guards=[IsAuthenticated()],
)
async def whoami(request):
    return {"user_id": request["context"]["user_id"]}


@api.get(
    "/dual",
    auth=[
        JWTAuthentication(secret=SECRET, cookie="access_token"),
        JWTAuthentication(secret=SECRET),
    ],
    guards=[IsAuthenticated()],
)
async def dual(request):
    return {"user_id": request["context"]["user_id"]}


@api.get(
    "/me",
    auth=[UsernameJWT(secret=SECRET, cookie="access_token")],
    guards=[IsAuthenticated()],
)
async def me(request):
    user = request.user
    return {"username": user.username if user else None}
