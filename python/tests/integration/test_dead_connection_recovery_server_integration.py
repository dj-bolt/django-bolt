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

import contextlib
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import jwt
import psycopg
import pytest

from .apps import app_module
from .apps.dead_connection_recovery import SECRET, USERNAME
from .helpers import _terminate_process, child_pids, postgres_settings

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


def start_server(
    make_server_project,
    params: dict[str, Any],
    *,
    orm_threads: int = 1,
    processes: int = 1,
    settings_extra: str = "",
    **database_options: Any,
):
    """Start ``runbolt`` on the given database, with a unique ``application_name``.

    ``PGAPPNAME`` is read by libpq, so the kill can be scoped to the
    connections of this server. One ORM thread keeps one connection parked
    between requests, so the shape of the failure is deterministic.
    """
    application_name = f"bolt-recovery-{secrets.token_hex(4)}"
    module = app_module("dead_connection_recovery")
    # runbolt loads one API for each BOLT_API entry. The lane routes are on a second API.
    base = module.split(":", 1)[0]
    both_apis = f"\nBOLT_API = [{module!r}, {base + ':lane_api'!r}]\n"
    project = make_server_project(
        api_module=module,
        settings_extra=postgres_settings(params, **database_options) + both_apis + settings_extra,
    )
    server = project.start(
        processes=processes, env={"PGAPPNAME": application_name, "DJANGO_BOLT_ORM_THREADS": str(orm_threads)}
    )
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


def test_the_async_user_loader_of_jwt_recovers_after_postgres_closes_the_connection(
    make_server_project, postgres_database
):
    """``JWTAuthentication.get_user`` is the async loader that a backend with its own ``get_user`` can call.

    Its query must run where Bolt drops a dead connection. Before, the query
    ran on a thread with no such check. Each request on that thread then
    failed until the process restarted.
    """
    server, application_name = start_server(make_server_project, postgres_database)
    with server:
        headers = seed_and_authenticate(server)
        before = server.get("/me-async-loader", headers=headers)
        assert before.status_code == 200, before.text
        assert before.json() == {"username": USERNAME}

        killed = terminate_backends(postgres_database, application_name)
        assert killed >= 1, "the server kept no connection open between requests"

        statuses = [server.get("/me-async-loader", headers=headers).status_code for _ in range(5)]
        assert 200 in statuses[:2], f"no recovery within two requests: {statuses}"
        assert statuses[2:] == [200, 200, 200], f"recovery did not hold: {statuses}"
        assert server.get("/me-async-loader", headers=headers).json() == {"username": USERNAME}


# The pool keeps more connections than the threads hold, so the kill also
# ends idle connections of the pool.
POOL = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 8, "max_size": 12}}
"""
# After the second failed check in one checkout, the pool sleeps 0.9 s or more.
RECONNECT_BUDGET = 0.5


def query_each_orm_thread(server, count: int) -> list[str]:
    response = server.get(f"/orm-threads?count={count}")
    assert response.status_code == 200, response.text
    return response.json()["results"]


@pytest.mark.parametrize(
    "settings_extra",
    [
        pytest.param("", id="persistent"),
        pytest.param(POOL, id="pool"),
    ],
)
def test_one_dead_connection_retires_the_connections_of_every_thread(
    make_server_project, postgres_database, settings_extra
):
    """A restart of PostgreSQL kills the connection of each thread at once.

    The first call that meets a dead connection fails. Every thread then
    reconnects before its next call, so no other call fails. Before, each
    thread failed one call of its own: here, three ORM threads and the thread
    that loads ``request.user``. With a pool, the reconnects must not meet the
    dead connections of the pool, or the pool sleeps before each next one.
    """
    orm_threads = 4
    server, application_name = start_server(
        make_server_project, postgres_database, orm_threads=orm_threads, settings_extra=settings_extra
    )
    with server:
        headers = seed_and_authenticate(server)
        assert query_each_orm_thread(server, orm_threads) == ["ok"] * orm_threads

        killed = terminate_backends(postgres_database, application_name)
        assert killed > orm_threads, "the server did not keep a connection on each thread"

        assert query_each_orm_thread(server, 1) == ["OperationalError"]
        started = time.monotonic()
        assert query_each_orm_thread(server, 1) == ["ok"]
        assert query_each_orm_thread(server, orm_threads) == ["ok"] * orm_threads
        took = time.monotonic() - started
        assert took < RECONNECT_BUDGET, f"the calls after the failure took {took:.1f} s: the pool slept"
        recovered = server.get("/me", headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == {"username": USERNAME}


class FreezableProxy:
    """A TCP proxy to PostgreSQL that can stop forwarding without closing a connection.

    A frozen connection takes the bytes of a query and never answers, as a
    connection behind a network partition does. Thaw forwards the held bytes.
    """

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._frozen = threading.Event()
        self._sockets: list[socket.socket] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def freeze(self) -> None:
        self._frozen.set()

    def thaw(self) -> None:
        self._frozen.clear()

    def close(self) -> None:
        self.thaw()
        for sock in [self._listener, *self._sockets]:
            sock.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._target)
            self._sockets += [client, upstream]
            for source, target in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._forward, args=(source, target), daemon=True).start()

    def _forward(self, source: socket.socket, target: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                while self._frozen.is_set():
                    time.sleep(0.02)
                target.sendall(data)
        except OSError:
            return
        # Pass the end of the stream on, as a direct connection does.
        with contextlib.suppress(OSError):
            target.shutdown(socket.SHUT_WR)


def test_a_dead_connection_does_not_wait_for_the_idle_connections_of_its_pool(make_server_project, postgres_database):
    """A partition can leave the idle connections of the pool unable to answer.

    The thread that finds a dead connection must not send anything on them:
    it would wait until TCP gives up, and that thread can be an event loop.
    """
    proxy = FreezableProxy(postgres_database["host"], int(postgres_database["port"]))
    params = {**postgres_database, "host": "127.0.0.1", "port": proxy.port}
    server, _ = start_server(make_server_project, params, settings_extra=POOL)
    try:
        with server:
            assert query_each_orm_thread(server, 1) == ["ok"]
            proxy.freeze()
            started = time.monotonic()
            try:
                response = server.get("/break-own-connection", timeout=10)
            except httpx.TimeoutException:
                pytest.fail("the request that met the dead connection waited on the frozen idle connections")
            took = time.monotonic() - started
            proxy.thaw()
            assert response.status_code == 500, response.text
            assert took < RECONNECT_BUDGET, f"the failed request took {took:.1f} s"
    finally:
        proxy.close()


# --- Connection pools: forks, slots, streams, and aliases -------------------


def backend_pids(params: dict[str, Any], application_name: str) -> set[int]:
    with psycopg.connect(**params, autocommit=True) as conn:
        rows = conn.execute("SELECT pid FROM pg_stat_activity WHERE application_name = %s", (application_name,))
        return {pid for (pid,) in rows.fetchall()}


FORK_POOL = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 4, "max_size": 16}}
"""


def wait_for_workers(server: Any, count: int) -> None:
    """Wait until ``count`` workers answer ``/echo``.

    The server is ready when one worker answers. Another worker can bind its
    socket later, and a keep-alive connection that opens before that bind
    always goes to the first worker. Each probe opens a new connection, so
    the kernel can send it to each worker that listens.
    """
    pids: set[int] = set()
    deadline = time.monotonic() + 30
    while len(pids) < count:
        assert time.monotonic() < deadline, f"only the workers {pids} answered"
        pids.add(httpx.get(server.url("/echo?token=probe"), timeout=10).json()["pid"])


# macOS does not spread connections across SO_REUSEPORT sockets: one worker
# serves each request, so the two workers never share a connection there.
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess load balancing only runs on Linux.")
def test_forked_workers_do_not_share_the_connections_of_the_parent(make_server_project, postgres_database):
    """The parent of ``runbolt`` opens the pool for the migration check, then forks its workers.

    A worker that inherits the sockets of that pool shares PostgreSQL sessions
    with the other worker, and each reads the query results of the other. Each
    token must come back to the request that sent it.
    """
    server, _ = start_server(make_server_project, postgres_database, processes=2, settings_extra=FORK_POOL)
    tokens = [secrets.token_hex(8) for _ in range(400)]
    # One client for each sender thread. httpcore does not make a client safe
    # to share between threads, and on a free-threaded build no GIL hides that.
    clients = threading.local()
    opened: list[httpx.Client] = []

    def send(token: str) -> tuple[str, int | None, Any]:
        if not hasattr(clients, "client"):
            clients.client = httpx.Client(timeout=10)
            opened.append(clients.client)
        try:
            response = clients.client.get(server.url(f"/echo?token={token}"))
        except httpx.HTTPError as exc:
            return token, None, type(exc).__name__
        return token, response.status_code, response.json() if response.status_code == 200 else response.text

    try:
        with server, ThreadPoolExecutor(32) as senders:
            wait_for_workers(server, 2)
            results = list(senders.map(send, tokens))
    finally:
        for client in opened:
            client.close()

    wrong = [result for result in results if result[1] != 200 or result[2]["token"] != result[0]]
    assert not wrong, f"{len(wrong)} of {len(tokens)} requests got no answer or another answer: {wrong[:3]}"
    assert len({body["pid"] for _, _, body in results}) == 2, "both workers must serve requests"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess load balancing only runs on Linux.")
def test_recycled_workers_do_not_share_the_connections_of_the_parent(make_server_project, postgres_database):
    """A recycled worker forks from the parent long after startup. It must open its own pool too.

    Each worker lives 2 s, so the parent forks new workers while the tokens
    go out. Each token must come back to the request that sent it.
    """
    project = make_server_project(
        api_module=app_module("dead_connection_recovery"),
        settings_extra=postgres_settings(postgres_database) + FORK_POOL,
    )
    server = project.start(processes=2, extra_args=["--workers-lifetime", "2", "--workers-kill-timeout", "5"])
    clients = threading.local()
    opened: list[httpx.Client] = []
    results: list[tuple[str, int | None, Any]] = []
    workers: set[int] = set()

    def send(token: str) -> tuple[str, int | None, Any]:
        if not hasattr(clients, "client"):
            clients.client = httpx.Client(timeout=10)
            opened.append(clients.client)
        # A recycled worker closes its idle keep-alive connections. Send the
        # request again on a new connection. A wrong token is never sent again.
        for _ in range(3):
            try:
                response = clients.client.get(server.url(f"/echo?token={token}"))
                break
            except httpx.TransportError as exc:
                error = type(exc).__name__
        else:
            return token, None, error
        return token, response.status_code, response.json() if response.status_code == 200 else response.text

    try:
        with server, ThreadPoolExecutor(32) as senders:
            workers.update(child_pids(server.process.pid))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                results += senders.map(send, [secrets.token_hex(8) for _ in range(100)])
                workers.update(child_pids(server.process.pid))
    finally:
        for client in opened:
            client.close()

    assert len(workers) > 2, f"expected at least one recycled worker, saw {workers}"
    wrong = [result for result in results if result[1] != 200 or result[2]["token"] != result[0]]
    assert not wrong, f"{len(wrong)} of {len(results)} requests got no answer or another answer: {wrong[:3]}"


# A backend on top of Django's PostgreSQL backend whose pool does not close.
UNCLOSABLE_POOL_BACKEND = """
from django.db.backends.postgresql.base import DatabaseWrapper as PostgreSQLWrapper


class DatabaseWrapper(PostgreSQLWrapper):
    def close_pool(self):
        raise RuntimeError("this pool does not close")
"""


def test_runbolt_forks_no_worker_when_a_pool_of_the_parent_does_not_close(make_server_project, postgres_database):
    """A worker that forks while a pool of the parent is open shares the connections of that pool.

    CPython reports an exception of a before-fork hook and forks anyway. The
    parent must stop with the error and fork no worker.
    """
    project = make_server_project(
        api_module=app_module("dead_connection_recovery"),
        settings_extra=postgres_settings(postgres_database, engine="unclosable") + FORK_POOL,
        extra_files={"unclosable/__init__.py": "", "unclosable/base.py": UNCLOSABLE_POOL_BACKEND},
    )
    process, port = project.spawn(processes=2)
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process(process)
        pytest.fail(f"runbolt forked its workers with a pool that did not close\nstderr:\n{stderr}")

    assert process.returncode != 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "RuntimeError: this pool does not close" in stderr, stderr
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)


ONE_CONNECTION_POOL = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 1, "max_size": 1, "timeout": 5}}
"""


def test_a_pooled_connection_goes_back_to_its_pool_after_each_call(make_server_project, postgres_database):
    """A thread that kept its pooled connection would hold a pool slot while it is idle.

    Two ORM threads share one connection here. The call on each thread gives
    the connection back when it ends, so the call on the other thread gets it.
    """
    server, _ = start_server(make_server_project, postgres_database, orm_threads=2, settings_extra=ONE_CONNECTION_POOL)
    with server:
        for _ in range(3):
            assert query_each_orm_thread(server, 2) == ["ok", "ok"]


def test_a_user_load_on_the_event_loop_gives_its_pooled_connection_back(make_server_project, postgres_database):
    """A handler with no await runs on the worker thread, and its lazy user load blocks that thread's loop.

    The load runs as one guarded call on that thread. It must give the
    connection back when it ends, or the one connection of the pool stays
    with the worker thread.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=ONE_CONNECTION_POOL)
    with server:
        headers = seed_and_authenticate(server)
        for _ in range(3):
            response = server.get("/me-inline", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == {"username": USERNAME}
            assert query_each_orm_thread(server, 1) == ["ok"]


def test_a_lane_keeps_its_pooled_connection_until_its_request_ends(make_server_project, postgres_database):
    """A request with Django middleware runs its ORM calls on its lane.

    The middleware checks out the one connection of the pool. The three calls
    of the handler use it, with no checkout of their own. The lane gives it
    back when the request ends, so the next request gets it.
    """
    project = make_server_project(
        api_module=app_module("request_lanes_async"),
        settings_extra=postgres_settings(postgres_database) + ONE_CONNECTION_POOL,
    )
    with project.start() as server:
        for _ in range(3):
            response = server.get("/pool-checkouts")
            assert response.status_code == 200, response.text
            assert response.json() == {"checkouts": 0}


def test_an_error_of_user_code_on_a_lane_keeps_its_pooled_connection(make_server_project, postgres_database):
    """A call that raises an error that is not a database error leaves a healthy connection.

    The lane keeps that connection until its request ends, as it does after a
    call that succeeds. Django gives a pooled connection a maximum age of 0,
    so a check of its age would give it back after each error.
    """
    project = make_server_project(
        api_module=app_module("request_lanes_async"),
        settings_extra=postgres_settings(postgres_database) + ONE_CONNECTION_POOL,
    )
    with project.start() as server:
        for _ in range(3):
            response = server.get("/pool-checkouts-after-an-error")
            assert response.status_code == 200, response.text
            assert response.json() == {"checkouts": 0}


def test_a_failed_return_to_the_pool_keeps_the_response_of_a_sync_lane(make_server_project, postgres_database):
    """A sync request behind Django middleware gives its pooled connection back when it ends. That return can fail.

    The failure must not replace the response of the request. Bolt logs it,
    and the lane serves its next request with a new connection.
    """
    project = make_server_project(
        api_module=app_module("request_lanes_async"),
        settings_extra=postgres_settings(postgres_database) + POOL,
    )
    server = project.start()
    with server:
        for _ in range(3):
            response = server.get("/lose-the-pool-on-a-sync-lane")
            assert response.status_code == 200, response.text
            assert response.json() == {"result": "ok"}
            response = server.get("/query-on-a-sync-lane")
            assert response.status_code == 200, response.text
            assert response.json() == {"result": 1}
    stdout, stderr = server.stop()
    assert "Could not give the pooled database connections of a lane back after its request" in stdout + stderr


# A pool that a checkout empties opens one more connection in the background,
# and pool_size counts it before it is available. The pool of ``second`` is at
# its max_size, so its statistics show only the checked-out connection.
TWO_POOLS = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 1, "max_size": 2, "timeout": 5}}
DATABASES["second"] = {**DATABASES["default"], "OPTIONS": {"pool": {"min_size": 1, "max_size": 1, "timeout": 5}}}
"""


def test_a_failed_close_on_a_lane_still_gives_back_its_other_pooled_connections(make_server_project, postgres_database):
    """A lane closes its connections that had an error, then gives back its pooled connections. The close can fail.

    That failure must not keep the connection of another pooled database on the
    lane. An idle lane would hold that pool slot until it stops.
    """
    project = make_server_project(
        api_module=app_module("request_lanes_async"),
        settings_extra=postgres_settings(postgres_database) + TWO_POOLS,
    )
    server = project.start()
    with server:
        response = server.get("/lose-the-pool-after-an-error-on-a-sync-lane")
        assert response.status_code == 200, response.text
        assert response.json() == {"result": "ok"}
        stats = server.get("/pool-stats?alias=second").json()
        assert stats["pool_size"] - stats["pool_available"] == 0, stats
    stdout, stderr = server.stop()
    assert "Could not close the unusable database connections of a lane after its request" in stdout + stderr


# ``a`` and ``b`` are at their max_size, for the reason of TWO_POOLS.
THREE_POOLS = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 1, "max_size": 2, "timeout": 5}}
DATABASES["a"] = {**DATABASES["default"], "OPTIONS": {"pool": {"min_size": 1, "max_size": 1, "timeout": 5}}}
DATABASES["b"] = {**DATABASES["default"], "OPTIONS": {"pool": {"min_size": 1, "max_size": 1, "timeout": 5}}}
"""


def test_a_failed_return_of_one_pooled_connection_still_gives_back_the_others(make_server_project, postgres_database):
    """A thread gives back its pooled connections one database at a time. One return can fail.

    That failure must not keep the connections of the databases after it on
    the thread. The hash seed fixes the order of the databases, so one of the
    two servers here fails the return of the first database in that order.
    """
    for broken, other in (("a", "b"), ("b", "a")):
        project = make_server_project(
            api_module=app_module("request_lanes_async"),
            settings_extra=postgres_settings(postgres_database) + THREE_POOLS,
        )
        server = project.start(env={"PYTHONHASHSEED": "0"})
        with server:
            response = server.get(f"/lose-the-pool-of-one-database-on-a-sync-lane?alias={broken}")
            assert response.status_code == 200, response.text
            stats = server.get(f"/pool-stats?alias={other}").json()
            assert stats["pool_size"] - stats["pool_available"] == 0, (broken, stats)
        stdout, stderr = server.stop()
        assert "Could not give the pooled database connections of a lane back after its request" in stdout + stderr


def test_a_pooled_connection_left_without_autocommit_goes_back_clean_and_keeps_the_pool(
    make_server_project, postgres_database
):
    """A call that turns autocommit off and raises leaves a connection that Django does not trust.

    Bolt gives that connection back to its pool, as Django does on
    ``request_finished``. The pool rolls back its transaction, and Django
    turns autocommit on again at the next checkout. The pool is not drained:
    the connection is healthy, and a drain would close each idle connection.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=ONE_CONNECTION_POOL)
    with server:
        for _ in range(3):
            response = server.get("/autocommit-off-then-fail")
            assert response.status_code == 200, response.text
            assert response.json() == {
                "autocommit": True,
                "transaction_status": "IDLE",
                "same_backend": True,
                "rolled_back": True,
            }


def test_a_failed_return_to_the_pool_keeps_the_result_of_the_call(make_server_project, postgres_database):
    """A call on the event loop gives its pooled connection back when it ends. That return can fail.

    The failure must not replace the result or the error of the call. Bolt
    logs it. After an error that leaves the connection unusable, the drop of
    the connection gives it back, so that is the return that fails.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=POOL)
    with server:
        assert server.get("/lose-the-pool").json() == {"result": "ok"}
        assert server.get("/lose-the-pool?fail=true").json() == {"result": "ValueError"}
        assert server.get("/lose-the-pool?kill=true").json() == {"result": "OperationalError"}
        assert query_each_orm_thread(server, 1) == ["ok"]
    stdout, stderr = server.stop()
    assert "Could not give the pooled database connections of a thread back after a call" in stdout + stderr
    assert "Could not close the unusable database connections of a thread after a call failed" in stdout + stderr


# A second database with persistent connections turns on the check before each call.
PERSISTENT_SECOND_DATABASE = """
DATABASES["persistent"] = {**DATABASES["default"], "CONN_MAX_AGE": 60, "OPTIONS": {}}
"""


@pytest.mark.parametrize(
    "settings_extra",
    [
        pytest.param(POOL, id="pool"),
        # Django gives a pooled connection a maximum age of 0. The check before
        # each call must leave it out, or the check gives it back before each call.
        pytest.param(POOL + PERSISTENT_SECOND_DATABASE, id="pool-and-a-persistent-database"),
    ],
)
def test_an_orm_thread_keeps_its_pooled_connection_while_calls_wait_for_it(
    make_server_project, postgres_database, settings_extra
):
    """A thread with queued work keeps its connection. It gives it back when its queue is empty.

    One ORM thread gets five calls at once. A checkout and a return for each
    call would cost a round trip to the pool each time.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=settings_extra)
    with server:
        response = server.get("/queued-orm-calls?count=5")
        assert response.status_code == 200, response.text
        assert response.json() == {"results": ["ok"] * 5, "checkouts": 1}


def test_the_default_executor_gives_its_pooled_connection_back(make_server_project, postgres_database):
    """``sync_to_async(thread_sensitive=False)`` runs on the default executor of the loop.

    The pool has one connection. When the executor thread kept it, the next
    call on the ORM pool would wait for the pool timeout and fail.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=ONE_CONNECTION_POOL)
    with server:
        for _ in range(3):
            response = server.get("/default-executor-query")
            assert response.status_code == 200, response.text
            assert response.json() == {"result": "ok"}
            assert query_each_orm_thread(server, 1) == ["ok"]


TWO_CONNECTION_POOL = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 1, "max_size": 2, "timeout": 5}}
"""


def test_a_streaming_body_gives_its_connection_back(make_server_project, postgres_database):
    """A sync streaming body runs on a thread of its own, for one response.

    A connection that the body opened would never go back to the pool, so
    each such response would hold one pool slot for the life of the process.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=TWO_CONNECTION_POOL)
    with server:
        for _ in range(4):
            response = server.get("/stream?rows=3", timeout=15)
            assert response.status_code == 200, response.text
            assert response.text == "1\n2\n3\n"
        assert query_each_orm_thread(server, 1) == ["ok"]


def two_databases(other_application_name: str) -> str:
    return f"""
DATABASES["default"]["CONN_MAX_AGE"] = 600
DATABASES["other"] = {{
    **DATABASES["default"],
    "OPTIONS": {{"application_name": {other_application_name!r}}},
}}
"""


def test_a_dead_connection_reconnects_the_connections_of_its_own_database_only(make_server_project, postgres_database):
    """A failover of one database must not close the connections to another one.

    Both aliases point at one PostgreSQL database here, under two
    ``application_name`` values, so the kill can end the connections of one alias.
    """
    other_application_name = f"bolt-other-{secrets.token_hex(4)}"
    server, application_name = start_server(
        make_server_project,
        postgres_database,
        orm_threads=2,
        settings_extra=two_databases(other_application_name),
    )
    with server:
        assert server.get("/orm-threads-both-databases?count=2").json()["results"] == ["ok", "ok"]
        other_before = backend_pids(postgres_database, other_application_name)
        assert len(other_before) == 2, "each ORM thread must keep a connection to the other database"

        terminate_backends(postgres_database, application_name)
        assert server.get("/orm-threads-both-databases?count=1").json()["results"] == ["OperationalError"]
        assert server.get("/orm-threads-both-databases?count=2").json()["results"] == ["ok", "ok"]

        assert backend_pids(postgres_database, other_application_name) == other_before


def test_a_pool_check_logs_a_warning_at_startup(make_server_project, postgres_database):
    """Bolt gives a pooled connection back after each call, so a pool check adds a round trip to each call."""
    server, _ = start_server(
        make_server_project, postgres_database, settings_extra=TWO_CONNECTION_POOL, CONN_HEALTH_CHECKS=True
    )
    with server:
        assert query_each_orm_thread(server, 1) == ["ok"]
    stdout, stderr = server.stop()
    assert "gives its connection pool a check callback through CONN_HEALTH_CHECKS" in stdout + stderr


# A backend on top of Django's PostgreSQL backend, as PostGIS is. Its ENGINE
# path does not contain "postgresql".
POSTGRESQL_BASED_BACKEND = "from django.db.backends.postgresql.base import DatabaseWrapper  # noqa: F401\n"


def test_a_pool_check_logs_a_warning_for_a_backend_based_on_postgresql(make_server_project, postgres_database):
    """PostGIS and other backends based on PostgreSQL get the startup checks of the pool too."""
    project = make_server_project(
        api_module=app_module("dead_connection_recovery"),
        settings_extra=postgres_settings(postgres_database, engine="pgbased", CONN_HEALTH_CHECKS=True)
        + TWO_CONNECTION_POOL,
        extra_files={"pgbased/__init__.py": "", "pgbased/base.py": POSTGRESQL_BASED_BACKEND},
    )
    server = project.start()
    with server:
        assert query_each_orm_thread(server, 1) == ["ok"]
    stdout, stderr = server.stop()
    assert "gives its connection pool a check callback through CONN_HEALTH_CHECKS" in stdout + stderr


def kill_backends(params: dict[str, Any], application_name: str) -> int:
    """Kill every backend of the server with this ``application_name`` and return at once.

    ``terminate_backends`` waits until no backend is left. Under load, the
    pool opens new connections at once, so that wait does not end.
    """
    with psycopg.connect(**params, autocommit=True) as conn:
        killed = conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = %s AND pid <> pg_backend_pid()",
            (application_name,),
        ).fetchall()
    return len(killed)


def pool_stats(server) -> dict[str, int]:
    response = server.get("/pool-stats")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_failed_return_on_an_idle_orm_worker_keeps_the_worker(make_server_project, postgres_database):
    """An ORM worker gives its pooled connection back when its queue is empty. That return can fail.

    The failure must not end the worker thread: the executor would not start
    a new one, and its slot in the pool of threads would be gone. Bolt logs it
    and the next call on the worker checks out a new connection.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=POOL)
    with server:
        for _ in range(3):
            response = server.get("/lose-the-pool-on-an-orm-thread")
            assert response.status_code == 200, response.text
            assert response.json() == {"result": "ok"}
            assert query_each_orm_thread(server, 1) == ["ok"]
    stdout, stderr = server.stop()
    assert "Could not give the pooled database connections of an idle worker back" in stdout + stderr


def test_a_failed_return_at_the_end_of_a_streaming_body_keeps_the_response(make_server_project, postgres_database):
    """The thread of a sync streaming body closes its connections when the body ends. That close can fail.

    The client has the complete body by then. The failure is logged and the
    next stream and the next query work.
    """
    server, _ = start_server(make_server_project, postgres_database, settings_extra=POOL)
    with server:
        for _ in range(3):
            response = server.get("/stream-and-lose-the-pool?rows=3", timeout=15)
            assert response.status_code == 200, response.text
            assert response.text == "1\n2\n3\n"
        assert server.get("/stream?rows=2", timeout=15).text == "1\n2\n"
        assert query_each_orm_thread(server, 1) == ["ok"]
    stdout, stderr = server.stop()
    assert "Error closing the database connections of a sync streaming thread" in stdout + stderr


SMALL_POOL = """
DATABASES["default"]["OPTIONS"] = {"pool": {"min_size": 1, "max_size": 4, "timeout": 5}}
"""


def test_the_pool_has_no_leaked_slot_after_backends_die_under_concurrent_load(make_server_project, postgres_database):
    """Backends die five times while 24 clients hit every connection path at once.

    The pool has four connections for four ORM threads, the event loop, the
    default executor and the streaming threads. Each path gives its connection
    back on its own error path. After the load, no connection stays checked
    out, every route answers, and the server logged no failed return.
    """
    server, application_name = start_server(
        make_server_project, postgres_database, orm_threads=4, settings_extra=SMALL_POOL
    )
    with server:
        headers = seed_and_authenticate(server)
        # /orm-threads holds its calls at a barrier of ``count`` parties. Two
        # requests with count=4 can share the four ORM threads and wait for
        # each other until the barrier times out, so the load uses count=1.
        paths = [
            "/orm-threads?count=1",
            "/me",
            "/me-inline",
            "/default-executor-query",
            "/echo?token=abc",
            "/stream?rows=3",
            "/queued-orm-calls?count=3",
            "/lane/query",
        ]
        stop = threading.Event()
        counts = {"ok": 0, "error": 0}
        counts_lock = threading.Lock()
        clients = threading.local()
        opened: list[httpx.Client] = []

        def hammer(index: int) -> None:
            clients.client = httpx.Client(timeout=10)
            opened.append(clients.client)
            while not stop.is_set():
                path = paths[index % len(paths)]
                index += 1
                try:
                    response = clients.client.get(server.url(path), headers=headers)
                    ok = response.status_code == 200
                except httpx.HTTPError:
                    ok = False
                with counts_lock:
                    counts["ok" if ok else "error"] += 1

        killed = []
        try:
            with ThreadPoolExecutor(24) as senders:
                futures = [senders.submit(hammer, index) for index in range(24)]
                try:
                    time.sleep(1.0)
                    for _ in range(5):
                        killed.append(kill_backends(postgres_database, application_name))
                        time.sleep(0.4)
                    time.sleep(1.0)
                finally:
                    # The senders stop on each exit of this block, or the executor never ends.
                    stop.set()
                for future in futures:
                    future.result()
        finally:
            for client in opened:
                client.close()
        assert all(killed), killed

        # A kill ends at most max_size connections. Each one fails the request
        # on it, and a dead idle connection can fail one more before the drain.
        assert counts["error"] <= 5 * 4 * 2, counts
        # The rate of requests depends on the machine, so this only shows that the load ran.
        assert counts["ok"] >= 100, counts
        # The load has ended. Every path gave its connection back.
        for _ in range(3):
            assert query_each_orm_thread(server, 4) == ["ok"] * 4
            for path in paths:
                response = server.get(path, headers=headers, timeout=15)
                assert response.status_code == 200, (path, response.text)
        deadline = time.monotonic() + 5
        while True:
            stats = pool_stats(server)
            checked_out = stats["pool_size"] - stats["pool_available"]
            if checked_out == 0 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert checked_out == 0, stats
        assert stats["requests_waiting"] == 0, stats
    stdout, stderr = server.stop()
    assert "Could not give the pooled database connections" not in stdout + stderr
    assert "Could not close the unusable database connections" not in stdout + stderr
