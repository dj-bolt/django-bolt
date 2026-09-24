from __future__ import annotations

from django.apps import AppConfig


class TenantsConfig(AppConfig):
    name = "tests.integration.apps._tenants"
    label = "bolt_tenants"
    default_auto_field = "django.db.models.BigAutoField"
