from __future__ import annotations

from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Client(TenantMixin):
    name = models.CharField(max_length=100)
    # save() creates the schema and runs the tenant migrations in it.
    auto_create_schema = True


class Domain(DomainMixin):
    pass
