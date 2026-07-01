"""Django-session login + user resolution for the ``/authorize`` browser step.

The Bolt server is a separate process from Django's WSGI app, so we don't rely on
Django's ``LoginView`` being reachable — we authenticate against the Django user model
and drive the configured session backend directly, reusing Django's password hashing,
session-key generation, and session-auth-hash (which honors password changes / logout).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    authenticate,
    get_user_model,
)
from django.utils.crypto import constant_time_compare


def _session_store_cls():
    return import_module(settings.SESSION_ENGINE).SessionStore


def session_cookie_name() -> str:
    return settings.SESSION_COOKIE_NAME


def session_cookie_kwargs() -> dict[str, Any]:
    """Cookie attributes for the session cookie, mirroring Django's own settings."""
    samesite = settings.SESSION_COOKIE_SAMESITE
    return {
        "max_age": settings.SESSION_COOKIE_AGE,
        "path": settings.SESSION_COOKIE_PATH,
        "domain": settings.SESSION_COOKIE_DOMAIN,
        "secure": bool(settings.SESSION_COOKIE_SECURE),
        "httponly": bool(settings.SESSION_COOKIE_HTTPONLY),
        # Django allows SESSION_COOKIE_SAMESITE=None to omit the attribute; our cookie
        # layer expects "Strict"/"Lax"/"None"/False, so map a Python None to "Lax".
        "samesite": samesite if samesite else "Lax",
    }


def origin_ok(request: Any, issuer: str) -> bool:
    """CSRF defense for the browser POST: require the Origin to match the issuer origin.

    ``Origin`` is a forbidden (``Sec-``-class) header browsers attach to cross-site POSTs
    and cannot be spoofed by page script, so an exact origin match is a robust check.
    Missing Origin is rejected (state-changing request).
    """
    origin = request.headers.get("origin")  # header keys arrive lowercase (canonicalized in Rust)
    if not origin:
        return False
    want = urlsplit(issuer)
    got = urlsplit(origin)
    return (got.scheme, got.netloc) == (want.scheme, want.netloc)


# ── sync cores ──────────────────────────────────────────────────────────────────
def _login(username: str, password: str) -> dict[str, Any] | None:
    user = authenticate(username=username, password=password)
    if user is None or not user.is_active:
        return None
    store = _session_store_cls()()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = user.backend  # set by authenticate()
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.create()
    return {"session_key": store.session_key, "user_id": str(user.pk), "username": user.get_username()}


def _user_from_session(session_key: str | None) -> dict[str, Any] | None:
    if not session_key:
        return None
    store = _session_store_cls()(session_key=session_key)
    uid = store.get(SESSION_KEY)
    if not uid:
        return None
    user = get_user_model().objects.filter(pk=uid).first()
    if user is None or not getattr(user, "is_active", False):
        return None
    session_hash = store.get(HASH_SESSION_KEY)
    if session_hash and not constant_time_compare(session_hash, user.get_session_auth_hash()):
        return None
    return {"user_id": str(user.pk), "username": user.get_username()}


def _load_user(user_id: str) -> Any | None:
    return get_user_model().objects.filter(pk=user_id).first()


login = sync_to_async(_login, thread_sensitive=True)
user_from_session = sync_to_async(_user_from_session, thread_sensitive=True)
load_user = sync_to_async(_load_user, thread_sensitive=True)
