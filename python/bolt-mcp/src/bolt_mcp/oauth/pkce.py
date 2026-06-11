"""PKCE (RFC 7636) S256 verification — the only challenge method we accept."""

from __future__ import annotations

import base64
import hashlib
import hmac


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def compute_s256(code_verifier: str) -> str:
    """``BASE64URL(SHA256(code_verifier))`` with no padding (RFC 7636 §4.6)."""
    return _b64url_nopad(hashlib.sha256(code_verifier.encode("ascii")).digest())


def verify_s256(code_verifier: str, code_challenge: str) -> bool:
    """Constant-time check that ``code_verifier`` matches the stored S256 challenge."""
    if not code_verifier or not code_challenge:
        return False
    try:
        computed = compute_s256(code_verifier)
    except UnicodeEncodeError:
        return False  # a non-ASCII verifier can never match an S256 challenge
    return hmac.compare_digest(computed, code_challenge)
