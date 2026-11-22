"""URL configuration with a path parameter before the admin site.

Each view of the default ``AdminSite`` refuses the ``tenant`` argument. A project
with this URL pattern thus has its own ``AdminSite``. This one has two views that
take the argument.
"""

from __future__ import annotations

from django.contrib.admin import AdminSite
from django.http import HttpResponse
from django.urls import path


class TenantAdminSite(AdminSite):
    """An admin site that serves one tenant for each first path segment."""

    def get_urls(self):
        return [
            path("", self.tenant_index, name="index"),
            path("login/", self.tenant_login, name="login"),
        ]

    def tenant_index(self, request, tenant):
        return HttpResponse(f"admin index of {tenant}")

    def tenant_login(self, request, tenant):
        return HttpResponse(f"admin login of {tenant}")


site = TenantAdminSite()

urlpatterns = [
    path("<slug:tenant>/admin/", site.urls),
]
