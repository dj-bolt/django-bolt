"""Request lanes with django-tenants on PostgreSQL, on a real ``runbolt`` server.

``TenantMainMiddleware`` sets the schema of the tenant on the connection of
the thread that runs it. The in-process lane tests use a ``threading.local``
stand-in. This module checks the real library: the schema on the connection
of the lane, the user query in the schema of the tenant, and lanes that the
server reuses for requests of another tenant.

Each tenant has one user with the same primary key and the name
``<tenant>-user``. A user loaded in the wrong schema thus has the wrong name.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to a PostgreSQL DSN whose role can
create databases. ``just test-pg`` starts a throwaway server with Docker and
runs these tests. Without the variable, they skip.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import jwt
import pytest

from .apps import app_module
from .apps.tenant_lanes import SECRET, TENANTS
from .helpers import postgres_settings

pytestmark = [pytest.mark.server_integration, pytest.mark.postgres]

_SHARED_APPS = [
    "django_tenants",
    "tests.integration.apps._tenants",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_bolt",
]
_TENANT_APPS = ["django.contrib.contenttypes", "django.contrib.auth"]

ROUTES = ("/sync/me", "/sync/current", "/async/me", "/async/current", "/async/sync-read")


def _tenant_settings(params: dict[str, Any]) -> str:
    return "\n".join(
        [
            postgres_settings(params, engine="django_tenants.postgresql_backend"),
            'DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)',
            f"SHARED_APPS = {_SHARED_APPS!r}",
            f"TENANT_APPS = {_TENANT_APPS!r}",
            "INSTALLED_APPS = SHARED_APPS + [app for app in TENANT_APPS if app not in SHARED_APPS]",
            'TENANT_MODEL = "bolt_tenants.Client"',
            'TENANT_DOMAIN_MODEL = "bolt_tenants.Domain"',
            # The readiness probe has no tenant host. It then uses the public schema.
            "SHOW_PUBLIC_IF_NO_TENANT_FOUND = True",
        ]
    )


def _start_tenant_server(make_server_project, params: dict[str, Any]):
    project = make_server_project(api_module=app_module("tenant_lanes"), settings_extra=_tenant_settings(params))
    # The middleware reads the domain table on each request, so the tables
    # and the tenants must exist before the readiness probe.
    project.manage("seed_tenants")
    return project.start()


def _headers(tenant: str) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode({"sub": "1", "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Host": f"{tenant}.localhost"}


def _check(server, route: str, tenant: str, client: httpx.Client | None = None) -> None:
    if client is None:
        response = server.get(route, headers=_headers(tenant))
    else:
        response = client.get(server.url(route), headers=_headers(tenant))
    assert response.status_code == 200, (route, tenant, response.text)
    body = response.json()
    assert body["username"] == f"{tenant}-user", (route, tenant, body)
    assert body["schema"] == tenant, (route, tenant, body)
    if route == "/async/me":
        assert body["orm_username"] == f"{tenant}-user", body
        assert body["tenant"] == tenant, body


def test_each_tenant_loads_its_own_user(make_server_project, postgres_database):
    """Each way to load the user reads the schema of the tenant of the request."""
    with _start_tenant_server(make_server_project, postgres_database) as server:
        for route in ROUTES:
            for tenant in (*TENANTS, *reversed(TENANTS)):
                _check(server, route, tenant)


def test_concurrent_requests_of_two_tenants_do_not_mix(make_server_project, postgres_database):
    """Lanes serve one request at a time and are reused. The state of one tenant must not leak."""
    # One client for each thread: the connection pool of httpx is not safe to
    # share between threads on free-threaded Python.
    clients = threading.local()
    opened: list[httpx.Client] = []

    def check_on_this_thread(case: tuple[str, str]) -> None:
        client = getattr(clients, "client", None)
        if client is None:
            client = clients.client = httpx.Client(timeout=10)
            opened.append(client)
        _check(server, *case, client=client)

    with _start_tenant_server(make_server_project, postgres_database) as server:
        cases = [(route, TENANTS[i % 2]) for i in range(120) for route in ROUTES]
        try:
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(check_on_this_thread, cases))
        finally:
            for client in opened:
                client.close()
