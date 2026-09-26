"""Migrate the database and create the tenants of ``tenant_lanes``.

Each tenant gets a schema, the domain ``<name>.localhost``, and one user
named ``<name>-user``. The user tables are per schema, so each user has the
same primary key, and only the schema tells them apart.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from tests.integration.apps.tenant_lanes import TENANTS


class Command(BaseCommand):
    help = "Create the tenants, domains, and users of the tenant_lanes app."

    def handle(self, *args, **options):
        call_command("migrate_schemas", shared=True, interactive=False, verbosity=0)
        tenant_model = get_tenant_model()
        domain_model = get_tenant_domain_model()
        for name in TENANTS:
            tenant = tenant_model(schema_name=name, name=name)
            tenant.save(verbosity=0)
            domain_model.objects.create(domain=f"{name}.localhost", tenant=tenant, is_primary=True)
            with schema_context(name):
                User.objects.create(username=f"{name}-user")
