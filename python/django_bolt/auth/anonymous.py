"""
The user of a request that has no user.

Rust imports this module on the first read of a ``request.user`` that no one
set. The Django app registry is then ready.
"""

from __future__ import annotations

from django.apps import apps

if apps.is_installed("django.contrib.auth"):
    from django.contrib.auth.models import AnonymousUser as new_anonymous_user
else:

    def new_anonymous_user() -> None:
        """Without ``django.contrib.auth``, there is no user model and no anonymous user."""
        return None


__all__ = ["new_anonymous_user"]
