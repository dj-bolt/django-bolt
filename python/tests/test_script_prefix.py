"""FORCE_SCRIPT_NAME: Bolt behind a proxy that removes a sub-path.

The proxy removes the sub-path from the request path, as for Django under
WSGI. Bolt then gives the paths and the script prefix that Django's
``WSGIHandler`` gives. Each test checks what user code sees through HTTP
responses of the test clients.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import threading
import time
import types
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Annotated
from wsgiref.util import setup_testing_defaults

import jwt
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse, JsonResponse
from django.urls import get_script_prefix, path, reverse, set_script_prefix

from django_bolt import BoltAPI, Depends, Request, StreamingResponse, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.concurrency import run_in_orm_executor, sync_to_thread
from django_bolt.exceptions import HTTPException
from django_bolt.middleware import BaseMiddleware, DjangoMiddlewareStack
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.spec import Server
from django_bolt.responses import JSON
from django_bolt.serializers import Serializer, computed_field
from django_bolt.testing import AsyncTestClient, TestClient, WebSocketTestClient

from .test_auto_streaming import AnextOnly, AsyncChunks, Chunks, NextOnly

_names = itertools.count()


@pytest.fixture
def mount_urlconf(settings):
    """Return a function that makes ``urlpatterns`` the ROOT_URLCONF, in a new module each time."""
    created: list[str] = []

    def make(urlpatterns: list) -> str:
        name = f"tests._script_prefix_urls_{next(_names)}"
        module = types.ModuleType(name)
        module.urlpatterns = urlpatterns
        sys.modules[name] = module
        created.append(name)
        settings.ROOT_URLCONF = name
        return name

    yield make
    for name in created:
        sys.modules.pop(name, None)


# --- The request ------------------------------------------------------------


def _request_facts(request, name: str, **kwargs) -> dict:
    return {
        "reverse": reverse(name, kwargs=kwargs),
        "path": request.path,
        "path_info": request.path_info,
        "SCRIPT_NAME": request.META["SCRIPT_NAME"],
        "PATH_INFO": request.META["PATH_INFO"],
        "full_path": request.get_full_path(),
        "absolute": request.build_absolute_uri(),
        "relative": request.build_absolute_uri("other"),
    }


def _django_facts_view(request, mission_id):
    return JsonResponse(_request_facts(request, "plain", mission_id=mission_id))


def _django_wsgi_get(path_info: str, query: str = "") -> tuple[int, bytes]:
    """GET through Django's own WSGIHandler, as a WSGI server behind a proxy that removed the script name."""
    environ = {"PATH_INFO": path_info, "QUERY_STRING": query, "SCRIPT_NAME": "", "SERVER_NAME": "testserver"}
    setup_testing_defaults(environ)
    statuses: list[str] = []
    # The handler sets the script prefix of this thread. Give the next test its own.
    previous = get_script_prefix()
    try:
        body = b"".join(WSGIHandler()(environ, lambda status, _headers, _exc_info=None: statuses.append(status)))
    finally:
        set_script_prefix(previous)
    return int(statuses[0].split()[0]), body


class _DjangoFactsMiddleware:
    """Send the paths of the Django request in a header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["x-django-facts"] = json.dumps(_request_facts(request, "missions:detail", mission_id=7))
        return response


@pytest.mark.django_db
@pytest.mark.parametrize("script_name", ["/myapp", "/myapp/"])
@pytest.mark.parametrize("handler_kind", ["sync", "async"])
def test_a_request_under_force_script_name_matches_a_django_wsgi_request(
    mount_urlconf, settings, script_name, handler_kind
):
    api = BoltAPI(namespace="missions", django_middleware=[f"{__name__}._DjangoFactsMiddleware"])

    def facts(request, mission_id: int) -> dict:
        facts = _request_facts(request, "missions:detail", mission_id=mission_id)
        # The mapping forms give the path of the client too.
        return {**facts, "item": request["path"], "get": request.get("path")}

    if handler_kind == "sync":
        api.get("/{mission_id}", name="detail")(facts)
    else:

        @api.get("/{mission_id}", name="detail")
        async def detail(request, mission_id: int) -> dict:
            return facts(request, mission_id)

    settings.FORCE_SCRIPT_NAME = script_name
    mount_urlconf([path("missions/", api.urls), path("plain/<int:mission_id>", _django_facts_view, name="plain")])

    with TestClient(api) as client:
        response = client.get("/missions/7?x=1")
        # The proxy removes the script name, so a path with it is not found, as with Django under WSGI.
        unstripped = client.get("/myapp/missions/7").status_code
    status, body = _django_wsgi_get("/plain/7", "x=1")
    django = json.loads(body)

    assert (status, _django_wsgi_get("/myapp/plain/7")[0], unstripped) == (200, 404, 404)
    assert django == {
        "reverse": "/myapp/plain/7",
        "path": "/myapp/plain/7",
        "path_info": "/plain/7",
        "SCRIPT_NAME": script_name,
        "PATH_INFO": "/plain/7",
        "full_path": "/myapp/plain/7?x=1",
        "absolute": "http://testserver/myapp/plain/7?x=1",
        "relative": "http://testserver/myapp/plain/other",
    }
    expected = {key: value.replace("plain", "missions") for key, value in django.items()}
    assert response.json() == {**expected, "item": "/myapp/missions/7", "get": "/myapp/missions/7"}
    # The Django request of a Django middleware has the same paths.
    assert json.loads(response.headers["x-django-facts"]) == expected


def test_without_the_setting_the_request_has_no_script_name():
    api = BoltAPI()

    @api.get("/facts")
    async def facts(request) -> dict:
        return {
            "path": request.path,
            "path_info": request.path_info,
            "SCRIPT_NAME": request.META["SCRIPT_NAME"],
            "item": request["path"],
            "prefix": get_script_prefix(),
        }

    with TestClient(api) as client:
        assert client.get("/facts").json() == {
            "path": "/facts",
            "path_info": "/facts",
            "SCRIPT_NAME": "",
            "item": "/facts",
            "prefix": "/",
        }


def test_the_paths_that_a_middleware_skips_are_route_paths(settings):
    class Marker(BaseMiddleware):
        exclude_paths = ["/health"]

        async def process_request(self, request):
            response = await self.get_response(request)
            response.headers["x-marker"] = "ran"
            return response

    api = BoltAPI(middleware=[Marker])

    @api.get("/health")
    async def health() -> dict:
        return {}

    @api.get("/items")
    async def items() -> dict:
        return {}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert "x-marker" not in client.get("/health").headers
        assert client.get("/items").headers["x-marker"] == "ran"


def test_the_trailing_slash_redirect_has_the_script_name(settings):
    api = BoltAPI()

    @api.get("/list")
    async def listing() -> dict:
        return {}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api, follow_redirects=False) as client:
        response = client.get("/list/?page=2")

    assert response.status_code == 308
    assert response.headers["location"] == "/myapp/list?page=2"


def test_the_request_of_an_error_that_rust_makes_has_the_script_name(settings):
    def formatter(error, request) -> JSON:
        return JSON({"path": request.path, "path_info": request.path_info}, status_code=error.status)

    api = BoltAPI(error_formatter=formatter)

    @api.get("/typed/{n}")
    async def typed(n: int) -> dict:
        return {}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert client.get("/missing").json() == {"path": "/myapp/missing", "path_info": "/missing"}
        assert client.get("/typed/x").json() == {"path": "/myapp/typed/x", "path_info": "/typed/x"}


def test_the_docs_load_the_schema_through_the_script_name(settings):
    api = BoltAPI()
    custom = BoltAPI(
        openapi_config=OpenAPIConfig(title="t", version="1", servers=[Server(url="https://api.example.com")])
    )

    for each in (api, custom):

        @each.get("/items/{item_id}")
        async def item(item_id: int) -> dict:
            return {"id": item_id}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        schema = client.get("/docs/openapi.json").json()
        page = client.get("/docs/rapidoc").text
    with TestClient(custom) as client:
        # A server that the user sets stays.
        custom_servers = client.get("/docs/openapi.json").json()["servers"]

    assert schema["servers"] == [{"url": "/myapp"}]
    assert list(schema["paths"]) == ["/items/{item_id}"]
    assert 'spec-url="/myapp/docs/openapi.json"' in page
    assert custom_servers == [{"url": "https://api.example.com"}]


def test_the_docs_of_one_api_follow_the_setting_of_each_client(settings):
    api = BoltAPI()

    @api.get("/items")
    async def items() -> list:
        return []

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert client.get("/docs/openapi.json").json()["servers"] == [{"url": "/myapp"}]
    settings.FORCE_SCRIPT_NAME = None
    with TestClient(api) as client:
        assert client.get("/docs/openapi.json").json()["servers"] == [{"url": "/"}]
        assert 'spec-url="/docs/openapi.json"' in client.get("/docs/rapidoc").text


def test_static_files_serve_at_the_path_that_the_proxy_gives(settings, tmp_path):
    # The proxy removes /myapp, as from every other path. WhiteNoise serves
    # STATIC_URL without FORCE_SCRIPT_NAME for the same reason.
    (tmp_path / "site.css").write_text("body {}")
    settings.FORCE_SCRIPT_NAME = "/myapp"
    settings.STATIC_URL = "/myapp/static/"
    settings.STATIC_ROOT = str(tmp_path)

    with TestClient(BoltAPI()) as client:
        assert client.get("/static/site.css").text == "body {}"
        assert client.get("/myapp/static/site.css").status_code == 404


def test_media_files_serve_at_the_path_that_the_proxy_gives(settings, tmp_path):
    (tmp_path / "photo.txt").write_text("photo")
    settings.FORCE_SCRIPT_NAME = "/myapp"
    settings.MEDIA_URL = "/myapp/media/"
    settings.MEDIA_ROOT = str(tmp_path)

    with TestClient(BoltAPI()) as client:
        assert client.get("/media/photo.txt").text == "photo"
        assert client.get("/myapp/media/photo.txt").status_code == 404


def test_a_mounted_api_reverses_with_the_script_name_and_serves_without_it(mount_urlconf, settings):
    api = BoltAPI(namespace="blog")

    @api.get("/posts/{post_id}", name="post")
    async def post(request, post_id: int) -> dict:
        return {"reverse": reverse("blog:post", kwargs={"post_id": post_id}), "path": request.path}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("blog/", api.urls)])

    with TestClient(api) as client:
        assert client.get("/blog/posts/3").json() == {"reverse": "/myapp/blog/posts/3", "path": "/myapp/blog/posts/3"}
        assert client.get("/myapp/blog/posts/3").status_code == 404
        assert client.get("/posts/3").status_code == 404


# --- mount_django -------------------------------------------------------------


@pytest.mark.django_db
def test_a_django_view_behind_mount_django_sees_the_paths_of_django_wsgi(mount_urlconf, settings):
    settings.FORCE_SCRIPT_NAME = "/myapp"
    mount_urlconf([path("plain/<int:mission_id>", _django_facts_view, name="plain")])
    api = BoltAPI()
    # As the admin mount: the URLconf patterns have the mount path.
    api.mount_django("/plain", clear_root_path=True)

    with TestClient(api) as client:
        through_bolt = client.get("/plain/7?x=1").json()
    status, body = _django_wsgi_get("/plain/7", "x=1")

    assert status == 200
    assert through_bolt == json.loads(body)


def _slash_view(request):
    return HttpResponse(request.path)


@pytest.mark.django_db
def test_a_django_redirect_behind_a_mount_with_a_root_path_has_the_script_name_one_time(mount_urlconf, settings):
    settings.FORCE_SCRIPT_NAME = "/site"
    settings.APPEND_SLASH = True
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
    mount_urlconf([path("legacy/page/", _slash_view)])
    api = BoltAPI()
    api.mount_django("/legacy")

    with TestClient(api, follow_redirects=False) as client:
        redirect = client.get("/legacy/page")
        page = client.get("/legacy/page/")

    assert redirect.status_code == 301
    assert redirect.headers["location"] == "/site/legacy/page/"
    assert page.text == "/site/legacy/page/"


def _hand_written_redirect(request):
    return HttpResponse(status=302, headers={"Location": "/other/"})


@pytest.mark.django_db
def test_a_root_relative_redirect_behind_a_mount_with_a_root_path_gets_the_mount_path(mount_urlconf, settings):
    settings.FORCE_SCRIPT_NAME = "/site"
    mount_urlconf([path("legacy/go", _hand_written_redirect)])
    api = BoltAPI()
    api.mount_django("/legacy")

    with TestClient(api, follow_redirects=False) as client:
        response = client.get("/legacy/go")

    assert response.headers["location"] == "/site/legacy/other/"


# --- The script prefix where user code runs ------------------------------------


def _prefix() -> str:
    return get_script_prefix()


class _PyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        before = _prefix()
        response = await self.get_response(request)
        response.headers["x-before"] = before
        response.headers["x-after"] = _prefix()
        return response


class _DjangoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        before = _prefix()
        response = self.get_response(request)
        response["x-before"] = before
        response["x-after"] = _prefix()
        return response


def _formatter(error, request) -> JSON:
    return JSON({"prefix": _prefix(), "status": error.status}, status_code=error.status)


def _sync_dependency() -> str:
    return _prefix()


async def _async_dependency() -> str:
    return _prefix()


class _UserRow(Serializer):
    id: int

    @computed_field
    def prefix(self) -> str:
        return _prefix()


def _all_users():
    return User.objects.all()


_SEEN: dict[str, str] = {}


def _build(**kwargs) -> BoltAPI:
    @asynccontextmanager
    async def lifespan(app):
        _SEEN["lifespan"] = _prefix()
        yield

    api = BoltAPI(lifespan=lifespan, **kwargs)

    @api.get("/sync")
    def sync_handler() -> dict:
        return {"p": _prefix()}

    @api.get("/async")
    async def async_handler() -> dict:
        before = _prefix()
        await asyncio.sleep(0.001)
        return {"p": before, "after_await": _prefix()}

    @api.get("/sync-orm")
    def sync_orm() -> dict:
        User.objects.exists()
        return {"p": _prefix()}

    @api.get("/async-orm")
    async def async_orm() -> dict:
        await User.objects.aexists()
        return {"p": _prefix()}

    @api.get("/queryset")
    def queryset(request) -> list[_UserRow]:
        # From a helper: the sync dispatch defers the QuerySet, and Rust loads it in a task of its own.
        return _all_users()

    @api.get("/deps")
    async def deps(a: Annotated[str, Depends(_sync_dependency)], b: Annotated[str, Depends(_async_dependency)]) -> dict:
        return {"sync": a, "async": b}

    @api.get("/deps-sync")
    def deps_sync(a: Annotated[str, Depends(_sync_dependency)]) -> dict:
        return {"sync": a}

    @api.get("/hops")
    async def hops() -> dict:
        return {
            "sync_to_thread": await sync_to_thread(_prefix),
            "run_in_orm_executor": await run_in_orm_executor(_prefix),
            "sync_to_async": await sync_to_async(_prefix)(),
            "sync_to_async_thread": await sync_to_async(_prefix, thread_sensitive=False)(),
        }

    @api.get("/hops-sync")
    def hops_sync() -> dict:
        async def inner() -> str:
            return _prefix()

        User.objects.exists()
        return {"async_to_sync": async_to_sync(inner)()}

    @api.get("/stream-sync")
    def stream_sync():
        def body():
            yield json.dumps({"p": _prefix()})

        return StreamingResponse(body(), media_type="application/json")

    @api.get("/stream-async")
    async def stream_async():
        async def body():
            await asyncio.sleep(0)
            yield json.dumps({"p": _prefix()})

        return StreamingResponse(body(), media_type="application/json")

    @api.get("/stream-sync-in-async")
    async def stream_sync_in_async():
        def body():
            yield json.dumps({"p": _prefix()})

        return StreamingResponse(body(), media_type="application/json")

    @api.get("/teapot")
    async def teapot() -> dict:
        raise HTTPException(status_code=418, detail="x")

    @api.get("/secure", auth=[APIKeyAuthentication(api_keys={"k"})], guards=[IsAuthenticated()])
    async def secure() -> dict:
        return {}

    @api.get("/typed/{n}")
    async def typed(n: int) -> dict:
        return {}

    return api


# The routes and the value of each response that holds a script prefix.
_PLAIN_ROUTES = {
    "/sync": lambda r: r.json(),
    "/async": lambda r: r.json(),
    "/sync-orm": lambda r: r.json(),
    "/async-orm": lambda r: r.json(),
    "/queryset": lambda r: r.json(),
    "/deps": lambda r: r.json(),
    "/deps-sync": lambda r: r.json(),
    "/hops": lambda r: r.json(),
    "/hops-sync": lambda r: r.json(),
    "/stream-sync": lambda r: r.json(),
    "/stream-async": lambda r: r.json(),
    "/stream-sync-in-async": lambda r: r.json(),
}
_FORMATTER_ROUTES = {
    # The handler raises: Python builds the error.
    "/teapot": lambda r: r.json()["prefix"],
    # Rust builds these errors: 401, 404 and 422.
    "/secure": lambda r: r.json()["prefix"],
    "/missing": lambda r: r.json()["prefix"],
    "/typed/x": lambda r: r.json()["prefix"],
}
_MIDDLEWARE_ROUTES = {
    route: lambda r: [r.headers["x-before"], r.headers["x-after"], r.json()] for route in _PLAIN_ROUTES
}

_CONFIGS = {
    "plain": ({}, _PLAIN_ROUTES),
    "formatter": ({"error_formatter": _formatter}, _FORMATTER_ROUTES),
    "python_middleware": ({"middleware": [_PyMiddleware]}, _MIDDLEWARE_ROUTES),
    "django_middleware": ({"django_middleware": [f"{__name__}._DjangoMiddleware"]}, _MIDDLEWARE_ROUTES),
}


def _prefixes(value) -> set[str]:
    """Every string in a JSON value."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*map(_prefixes, value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*map(_prefixes, value)) if value else set()
    return set()


def _seen(api: BoltAPI, routes: dict) -> dict[str, set[str]]:
    with TestClient(api, raise_server_exceptions=False) as client:
        return {route: _prefixes(read(client.get(route))) for route, read in routes.items()}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("config", list(_CONFIGS))
def test_reverse_has_the_script_prefix_where_user_code_runs(settings, config):
    kwargs, routes = _CONFIGS[config]
    User.objects.create(username="row")
    _SEEN.clear()

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with_setting = _seen(_build(**kwargs), routes)
    lifespan = _SEEN.pop("lifespan")
    settings.FORCE_SCRIPT_NAME = None
    without_setting = _seen(_build(**kwargs), routes)

    assert (with_setting, lifespan) == ({route: {"/myapp/"} for route in routes}, "/myapp/")
    assert without_setting == {route: {"/"} for route in routes}
    assert (get_script_prefix(), _SEEN["lifespan"]) == ("/", "/")


@pytest.mark.django_db(transaction=True)
def test_an_async_test_client_gives_the_script_prefix(settings):
    settings.FORCE_SCRIPT_NAME = "/myapp"

    async def run() -> list:
        async with AsyncTestClient(_build()) as client:
            return [_prefixes((await client.get(route)).json()) for route in ("/sync", "/async", "/hops")]

    assert asyncio.run(run()) == [{"/myapp/"}] * 3


@pytest.mark.parametrize(
    ("annotation", "body"),
    [
        (Iterable[bytes], Chunks),
        (AsyncIterable[bytes], AsyncChunks),
        (AsyncIterator[bytes], AnextOnly),
        (Iterator[bytes], NextOnly),
    ],
    ids=["iterable", "async-iterable", "anext-only", "next-only"],
)
def test_a_body_of_each_iterator_kind_streams_with_the_script_prefix(settings, annotation, body):
    # The body with the script prefix iterates the content as Rust does.
    api = BoltAPI()

    def stream():
        return body()

    stream.__annotations__["return"] = annotation
    api.get("/stream")(stream)

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert client.get("/stream").content == b"ab"


def _ws_dependency() -> str:
    return _prefix()


def _websocket_api() -> BoltAPI:
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, dep: Annotated[str, Depends(_ws_dependency)]):
        await websocket.accept()
        before = _prefix()
        await asyncio.sleep(0)
        await websocket.send_json({"handler": before, "after_await": _prefix(), "dependency": dep})
        await websocket.close()

    return api


@pytest.mark.asyncio
async def test_a_websocket_handler_has_the_script_prefix(settings):
    messages = []
    for script_name in ("/myapp", None):
        settings.FORCE_SCRIPT_NAME = script_name
        async with WebSocketTestClient(_websocket_api(), "/ws") as ws:
            messages.append(await ws.receive_json())

    assert messages == [
        {"handler": "/myapp/", "after_await": "/myapp/", "dependency": "/myapp/"},
        {"handler": "/", "after_await": "/", "dependency": "/"},
    ]
    assert get_script_prefix() == "/"


_JWT_SECRET = "script-prefix-test-secret-longer-than-32-chars"


class _PrefixAuth(JWTAuthentication):
    def get_user_sync(self, user_id):
        return SimpleNamespace(prefix=_prefix())


class _PassMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


def test_a_user_that_loads_on_the_lane_of_the_request_has_the_script_prefix(settings):
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_PassMiddleware])])

    @api.get("/user", auth=[_PrefixAuth(secret=_JWT_SECRET)])
    async def user(request: Request) -> dict:
        await asyncio.sleep(0)
        # A sync read on the loop loads the user on the lane of the request.
        return {"prefix": request.user.prefix}

    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, _JWT_SECRET, algorithm="HS256")
    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api, timeout=10) as client:
        assert client.get("/user", headers={"Authorization": f"Bearer {token}"}).json() == {"prefix": "/myapp/"}


# --- No leak ----------------------------------------------------------------------

_LEAK_ROUTES = [
    "/sync",
    "/async",
    "/sync-orm",
    "/async-orm",
    "/deps",
    "/deps-sync",
    "/hops",
    "/stream-sync",
    "/stream-async",
]


def _hammer(api: BoltAPI, requests: int, concurrency: int) -> set[str]:
    """Send ``requests`` requests from ``concurrency`` threads over the routes. Return every prefix that they saw."""
    seen: set[str] = set()
    lock = threading.Lock()
    routes = itertools.cycle(_LEAK_ROUTES)
    with TestClient(api, raise_server_exceptions=False) as client:

        def one(route: str) -> None:
            response = client.get(route)
            found = _prefixes(response.json()) | {response.headers["x-before"], response.headers["x-after"]}
            with lock:
                seen.update(found)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, [next(routes) for _ in range(requests)]))
    return seen


@pytest.mark.django_db(transaction=True)
def test_the_script_prefix_of_one_client_never_reaches_another(settings):
    User.objects.create(username="row")
    rounds = []
    for _ in range(3):
        for script_name, middleware in (("/myapp", "python"), (None, "django"), ("/other", "django"), (None, "python")):
            settings.FORCE_SCRIPT_NAME = script_name
            config = (
                {"middleware": [_PyMiddleware]}
                if middleware == "python"
                else {"django_middleware": [f"{__name__}._DjangoMiddleware"]}
            )
            rounds.append((script_name, _hammer(_build(**config), requests=700, concurrency=200)))

    assert rounds == [
        (script_name, {"/" if script_name is None else f"{script_name}/"}) for script_name, _seen_set in rounds
    ]
    assert get_script_prefix() == "/"
