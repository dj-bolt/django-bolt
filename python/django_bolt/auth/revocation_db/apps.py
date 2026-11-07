from __future__ import annotations

from django.apps import AppConfig


class RevocationDBConfig(AppConfig):
    name = "django_bolt.auth.revocation_db"
    label = "bolt_revocation"
    verbose_name = "Bolt token revocation"
    default_auto_field = "django.db.models.BigAutoField"
