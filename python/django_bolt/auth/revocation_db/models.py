from __future__ import annotations

from django.db import models


class RevokedToken(models.Model):
    """One row for each revoked or used token, revoked family, and user version.

    ``DjangoORMRevocation`` writes these rows. A family row has the ``jti`` ``family:<fam>``.
    A user version row has the ``jti`` ``version:<user_id>`` and does not expire.
    """

    jti = models.CharField(max_length=255, unique=True)
    revoked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    version = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return self.jti
