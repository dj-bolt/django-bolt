"""Async-safe persistence for OAuth state.

Handlers are ``async``; Django ORM access is sync, so every DB operation runs through
``sync_to_async(..., thread_sensitive=True)``. Secrets are hashed before they touch the
database and looked up by hash. Authorization codes are single-use (atomic delete on
read) and refresh tokens rotate with reuse detection.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from .models import AuthorizationCode, OAuthClient, RefreshToken
from .tokens import new_secret, sha256_hex


# ── clients (Dynamic Client Registration) ──────────────────────────────────────
def _create_client(client_name: str, redirect_uris: list[str], grant_types: list[str], scope: str) -> dict[str, Any]:
    client = OAuthClient.objects.create(
        client_id=new_secret(24),
        client_name=client_name or "",
        redirect_uris=list(redirect_uris),
        grant_types=list(grant_types) or ["authorization_code", "refresh_token"],
        scope=scope or "",
    )
    return {
        "pk": client.pk,
        "client_id": client.client_id,
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": client.grant_types,
        "scope": client.scope,
        "token_endpoint_auth_method": client.token_endpoint_auth_method,
    }


def _get_client(client_id: str) -> dict[str, Any] | None:
    client = OAuthClient.objects.filter(client_id=client_id).first()
    if client is None:
        return None
    return {
        "pk": client.pk,
        "client_id": client.client_id,
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "scope": client.scope,
    }


# ── authorization codes (single-use) ────────────────────────────────────────────
def _create_authorization_code(
    client_pk: int,
    user_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
    ttl: int,
) -> str:
    raw = new_secret()
    AuthorizationCode.objects.create(
        code_hash=sha256_hex(raw),
        client_id=client_pk,
        user_id=user_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope or "",
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    return raw


def _consume_authorization_code(raw_code: str) -> dict[str, Any] | None:
    """Atomically claim a code: read it, then delete by pk. A concurrent replay loses
    the delete race (``deleted == 0``) and gets ``None`` — single-use under concurrency."""
    row = AuthorizationCode.objects.filter(code_hash=sha256_hex(raw_code)).select_related("client").first()
    if row is None:
        return None
    deleted, _ = AuthorizationCode.objects.filter(pk=row.pk).delete()
    if not deleted:
        return None
    if row.expires_at <= timezone.now():
        return None
    return {
        "client_pk": row.client_id,
        "client_id": row.client.client_id,
        "user_id": row.user_id,
        "redirect_uri": row.redirect_uri,
        "code_challenge": row.code_challenge,
        "code_challenge_method": row.code_challenge_method,
        "scope": row.scope,
    }


# ── refresh tokens (rotation + reuse detection) ─────────────────────────────────
def _issue_refresh_token(client_pk: int, user_id: str, scope: str, ttl: int) -> str:
    raw = new_secret()
    RefreshToken.objects.create(
        token_hash=sha256_hex(raw),
        chain_id=new_secret(16),
        client_id=client_pk,
        user_id=user_id,
        scope=scope or "",
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    return raw


def _rotate_refresh_token(raw_token: str, ttl: int) -> tuple[str, dict[str, Any] | None]:
    with transaction.atomic():
        row = RefreshToken.objects.filter(token_hash=sha256_hex(raw_token)).select_related("client").first()
        if row is None:
            return "invalid", None
        if row.rotated:
            # An already-rotated token is being replayed → assume theft, revoke the family.
            RefreshToken.objects.filter(chain_id=row.chain_id).delete()
            return "reuse", None
        if row.expires_at <= timezone.now():
            return "expired", None
        updated = RefreshToken.objects.filter(pk=row.pk, rotated=False).update(rotated=True)
        if not updated:
            RefreshToken.objects.filter(chain_id=row.chain_id).delete()
            return "reuse", None
        raw_new = new_secret()
        RefreshToken.objects.create(
            token_hash=sha256_hex(raw_new),
            chain_id=row.chain_id,
            client=row.client,
            user_id=row.user_id,
            scope=row.scope,
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return "ok", {
            "user_id": row.user_id,
            "scope": row.scope,
            "client_id": row.client.client_id,
            "raw_new": raw_new,
        }


def _revoke_refresh_token(raw_token: str) -> bool:
    deleted, _ = RefreshToken.objects.filter(token_hash=sha256_hex(raw_token)).delete()
    return bool(deleted)


# ── async wrappers ──────────────────────────────────────────────────────────────
create_client = sync_to_async(_create_client, thread_sensitive=True)
get_client = sync_to_async(_get_client, thread_sensitive=True)
create_authorization_code = sync_to_async(_create_authorization_code, thread_sensitive=True)
consume_authorization_code = sync_to_async(_consume_authorization_code, thread_sensitive=True)
issue_refresh_token = sync_to_async(_issue_refresh_token, thread_sensitive=True)
rotate_refresh_token = sync_to_async(_rotate_refresh_token, thread_sensitive=True)
revoke_refresh_token = sync_to_async(_revoke_refresh_token, thread_sensitive=True)
