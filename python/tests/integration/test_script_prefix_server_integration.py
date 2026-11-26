"""A real runbolt under ``FORCE_SCRIPT_NAME``, behind a proxy that removes the sub-path.

The proxy removes ``/myapp``, so the server gets ``/sync`` for ``/myapp/sync``.
The test client cannot show these: the script prefix of ``django.setup()`` on
the main thread, the worker threads and request lanes of the server, a sync
streaming body on its own thread, and a WebSocket handshake.
"""

from __future__ import annotations

import itertools
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration

_ROUTES = ["/sync", "/async", "/orm", "/async-orm", "/hops", "/stream-sync", "/stream-async"]
_ALL_ROUTES = [*_ROUTES, *(f"/dj{route}" for route in _ROUTES)]


def _project(make_server_project, tmp_path, script_name: str | None):
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "site.css").write_text("body {}")
    settings = f"STATIC_URL = 'static/'\nSTATIC_ROOT = {str(static_root)!r}\n"
    if script_name is not None:
        settings += f"FORCE_SCRIPT_NAME = {script_name!r}\n"
    project = make_server_project(api_module=app_module("script_prefix"), settings_extra=settings)
    project.manage("migrate", "--run-syncdb")
    return project


def _prefixes(response: httpx.Response) -> set[str]:
    """Every script prefix in a response: the values of the body, and the header of the Django middleware."""
    found = {value for value in response.json().values() if value.endswith("/")}
    if "x-django-prefix" in response.headers:
        found.add(response.headers["x-django-prefix"])
    return found


def test_runbolt_gives_the_script_name_where_user_code_runs(make_server_project, tmp_path):
    project = _project(make_server_project, tmp_path, "/myapp")

    with project.start() as server:
        seen = {route: _prefixes(server.get(route)) for route in _ALL_ROUTES}
        sync = server.get("/sync").json()
        lane = server.get("/dj/sync")
        missing = server.get("/missing")
        lifespan = server.get("/lifespan").json()
        schema = server.get("/docs/openapi.json").json()
        static = server.get("/static/site.css")
        unstripped_static = server.get("/myapp/static/site.css").status_code
        with SimpleWebSocketClient(server.host, server.port, "/ws") as websocket:
            message = json.loads(websocket.receive_text())

    assert seen == {route: {"/myapp/"} for route in _ALL_ROUTES}
    assert sync == {"prefix": "/myapp/", "path": "/myapp/sync", "path_info": "/sync"}
    assert lane.headers["x-django-path"] == "/myapp/dj/sync"
    # The formatter of an error that Rust makes.
    assert (missing.status_code, missing.json()) == (404, {"prefix": "/myapp/", "path": "/myapp/missing"})
    # django.setup() gives the main thread the prefix, and the lifespan runs there.
    assert lifespan == {"prefix": "/myapp/"}
    assert schema["servers"] == [{"url": "/myapp"}]
    # Django puts the script name in front of a relative STATIC_URL. The proxy removes it.
    assert (static.text, unstripped_static) == ("body {}", 404)
    assert message == {"handler": "/myapp/", "dependency": "/myapp/"}


def _hammer(base_url: str, requests: int, concurrency: int) -> set[str]:
    """Send ``requests`` requests over every route from ``concurrency`` threads. Return every prefix that they saw."""
    seen: set[str] = set()
    lock = threading.Lock()
    routes = itertools.cycle(_ALL_ROUTES)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    with httpx.Client(base_url=base_url, limits=limits, timeout=30) as client:

        def one(route: str) -> None:
            response = client.get(route)
            response.raise_for_status()
            found = _prefixes(response)
            with lock:
                seen.update(found)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, [next(routes) for _ in range(requests)]))
    return seen


@pytest.mark.parametrize(("script_name", "prefix"), [("/myapp", "/myapp/"), (None, "/")])
def test_runbolt_gives_each_request_the_prefix_of_its_server_under_load(
    make_server_project, tmp_path, script_name, prefix
):
    project = _project(make_server_project, tmp_path, script_name)

    with project.start(extra_args=["--workers", "4"]) as server:
        base_url = f"http://{server.host}:{server.port}"
        rounds = [_hammer(base_url, requests=700, concurrency=200) for _ in range(3)]

    assert rounds == [{prefix}] * 3
