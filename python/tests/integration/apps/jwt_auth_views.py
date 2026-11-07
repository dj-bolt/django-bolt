"""JWTAuthViews with the revocation store that the setting ``REVOCATION_STORE`` names.

``"cache"`` uses ``DjangoCacheRevocation`` on the ``default`` cache. ``"database"`` uses
``DjangoORMRevocation`` on the table of ``django_bolt.auth.revocation_db``.
"""

from __future__ import annotations

import os

from django.conf import settings

from django_bolt import BoltAPI
from django_bolt.auth import DjangoCacheRevocation, DjangoORMRevocation, JWTAuthViews

SECRET = "jwt-auth-views-server-integration-secret"
STORES = {"cache": DjangoCacheRevocation, "database": DjangoORMRevocation}

api = BoltAPI()


class User:
    pk = "server-user"
    is_active = True


async def validate(request, credentials):
    return User() if credentials.password == "correct" else None


JWTAuthViews(
    store=STORES[getattr(settings, "REVOCATION_STORE", "cache")](),
    secret=SECRET,
    secure_cookies=False,
    credential_validator=validate,
).register(api)


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/pid")
async def pid():
    """Give the worker process ID, so a test can see more than one process answer."""
    return {"pid": os.getpid()}
