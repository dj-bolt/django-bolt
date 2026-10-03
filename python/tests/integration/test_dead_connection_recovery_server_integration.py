"""Recovery from dead PostgreSQL connections on a real ``runbolt`` server.

Bolt keeps Django connections open across requests and does not run
``close_old_connections``. The connection that loads ``request.user`` is
parked on an ORM pool thread between requests. Before 0.12, when PostgreSQL
closed that connection, every authenticated request on that thread failed with
``OperationalError: the connection is closed`` until the process restarted.

The connection has to survive between requests on a thread the framework
owns, so this needs a real server: ``TestClient`` runs the load in-process.
The in-process suite in ``test_broken_connection_recovery.py`` pins the same
code path against SQLite and covers the ``_check_before_call`` gate.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to a PostgreSQL DSN whose role can
create databases, for example
``postgresql://postgres:postgres@127.0.0.1:5432/postgres``. ``just test-pg``
starts a throwaway server with Docker and runs these tests. Without the
variable, they skip.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
import psycopg
import pytest

from .apps import app_module
from .apps.dead_connection_recovery import SECRET, USERNAME
from .helpers import postgres_settings

pytestmark = [pytest.mark.server_integration, pytest.mark.postgres]


def make_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")


def terminate_backends(params: dict[str, Any], application_name: str, timeout: float = 10.0) -> int:
    """Kill every backend of the server with this ``application_name``.

    Returns the number of backends killed, after they are gone from
    ``pg_stat_activity``. ``pg_terminate_backend`` returns before the backend
    exits, and a request that arrives first would still be served.
    """
    with psycopg.connect(**params, autocommit=True) as conn:
        killed = conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = %s AND pid <> pg_backend_pid()",
            (application_name,),
        ).fetchall()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            (remaining,) = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s AND pid <> pg_backend_pid()",
                (application_name,),
            ).fetchone()
            if remaining == 0:
                return len(killed)
            time.sleep(0.05)
    raise AssertionError(f"{remaining} backend(s) of {application_name!r} still alive after {timeout}s")


def start_server(make_server_project, params: dict[str, Any], **database_options: Any):
    """Start ``runbolt`` on the given database, with a unique ``application_name``.

    ``PGAPPNAME`` is read by libpq, so the kill can be scoped to the
    connections of this server. One ORM thread keeps one connection parked
    between requests, so the shape of the failure is deterministic.
    """
    application_name = f"bolt-recovery-{secrets.token_hex(4)}"
    project = make_server_project(
        api_module=app_module("dead_connection_recovery"),
        settings_extra=postgres_settings(params, **database_options),
    )
    server = project.start(env={"PGAPPNAME": application_name, "DJANGO_BOLT_ORM_THREADS": "1"})
    return server, application_name


def seed_and_authenticate(server) -> dict[str, str]:
    seeded = server.request("POST", "/seed")
    assert seeded.status_code == 200, seeded.text
    headers = {"Authorization": f"Bearer {make_token(seeded.json()['user_id'])}"}
    before = server.get("/me", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json() == {"username": USERNAME}
    return headers


def test_authenticated_requests_recover_after_postgres_closes_the_connection(make_server_project, postgres_database):
    """Default settings: neither ``CONN_MAX_AGE`` nor ``CONN_HEALTH_CHECKS``.

    Nothing checks the connection before the query, so the first request
    after the kill may fail: the connection looks fine until it is used. Bolt
    drops it on that error, and the request after it reconnects. Without the
    drop, every request on the thread fails until the process restarts.
    """
    server, application_name = start_server(make_server_project, postgres_database)
    with server:
        headers = seed_and_authenticate(server)

        killed = terminate_backends(postgres_database, application_name)
        assert killed >= 1, "the server kept no connection open between requests"

        statuses = [server.get("/me", headers=headers).status_code for _ in range(5)]
        assert 200 in statuses[:2], f"no recovery within two requests: {statuses}"
        assert statuses[2:] == [200, 200, 200], f"recovery did not hold: {statuses}"

        recovered = server.get("/me", headers=headers)
        assert recovered.json() == {"username": USERNAME}


def test_health_checks_replace_the_dead_connection_before_the_query(make_server_project, postgres_database):
    """``CONN_HEALTH_CHECKS`` turns on the check before each pool call.

    Django runs its health check one time after ``close_if_unusable_or_obsolete``
    re-arms it. Bolt runs that on the pool thread before each call, so the dead
    connection is found and replaced before the query, and no request fails.
    """
    server, application_name = start_server(
        make_server_project, postgres_database, CONN_MAX_AGE=600, CONN_HEALTH_CHECKS=True
    )
    with server:
        headers = seed_and_authenticate(server)

        killed = terminate_backends(postgres_database, application_name)
        assert killed >= 1, "the server kept no connection open between requests"

        first = server.get("/me", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json() == {"username": USERNAME}
        assert server.get("/me", headers=headers).status_code == 200
