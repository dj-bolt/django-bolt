"""ORM models for the OAuth Authorization Server.

Secrets are never stored raw: the authorization code and refresh token are
high-entropy values returned to the client once and persisted only as their SHA-256
hash, so a database leak yields nothing replayable. Persisting in the ORM (vs memory)
is what lets a registered client + refresh token survive a server restart or a
multi-process (SO_REUSEPORT) deployment — the reason connectors stay linked.
"""

from __future__ import annotations

from django.db import models


class OAuthClient(models.Model):
    """A client registered via Dynamic Client Registration (RFC 7591).

    Public client (PKCE, no secret). ``client_id`` is a high-entropy random
    identifier; it is public, so it is not hashed.
    """

    client_id = models.CharField(max_length=255, unique=True, db_index=True)
    client_name = models.CharField(max_length=255, blank=True, default="")
    redirect_uris = models.JSONField(default=list)
    grant_types = models.JSONField(default=list)
    scope = models.TextField(blank=True, default="")
    token_endpoint_auth_method = models.CharField(max_length=64, default="none")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.client_name or 'client'} ({self.client_id})"


class AuthorizationCode(models.Model):
    """A short-lived, single-use authorization code bound to a PKCE challenge."""

    code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="codes")
    user_id = models.CharField(max_length=255)
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=255)
    code_challenge_method = models.CharField(max_length=10, default="S256")
    scope = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"code {self.code_hash[:8]}… for user {self.user_id} (expires {self.expires_at})"


class RefreshToken(models.Model):
    """A refresh token (opaque, stored hashed) with rotation + reuse detection.

    Every rotation issues a new row sharing the original ``chain_id`` and flips the
    predecessor's ``rotated`` flag. Presenting an already-rotated token is treated as
    theft: the whole ``chain_id`` family is revoked.
    """

    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    chain_id = models.CharField(max_length=64, db_index=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="refresh_tokens")
    user_id = models.CharField(max_length=255)
    scope = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    rotated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"refresh token {self.token_hash[:8]}… for user {self.user_id} (expires {self.expires_at})"
