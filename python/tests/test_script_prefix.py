"""FORCE_SCRIPT_NAME: Bolt behind a proxy that removes a sub-path.

The proxy removes the sub-path from the request path, as for Django under
WSGI. Bolt then gives the paths and the script prefix that Django's
``WSGIHandler`` gives. Each test checks what user code sees through HTTP
responses of the test clients.
"""

from __future__ import annotations

import asyncio
import contextvars
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
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import get_script_prefix, path, reverse, reverse_lazy, set_script_prefix
from django.utils import translation
from django.utils.deprecation import MiddlewareMixin

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


def _django_wsgi_response(path_info: str, query: str = "") -> tuple[int, dict[str, str], bytes]:
    """GET through Django's own WSGIHandler, as a WSGI server behind a proxy that removed the script name."""
    environ = {"PATH_INFO": path_info, "QUERY_STRING": query, "SCRIPT_NAME": "", "SERVER_NAME": "testserver"}
    setup_testing_defaults(environ)
    started: list[tuple[str, list]] = []
    # The handler sets the script prefix of this thread. Give the next test its own.
    previous = get_script_prefix()
    try:
        body = b"".join(
            WSGIHandler()(environ, lambda status, headers, _exc_info=None: started.append((status, headers)))
        )
    finally:
        set_script_prefix(previous)
    status, headers = started[0]
    return int(status.split()[0]), {name.lower(): value for name, value in headers}, body


def _django_wsgi_get(path_info: str, query: str = "") -> tuple[int, bytes]:
    status, _headers, body = _django_wsgi_response(path_info, query)
    return status, body


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


class _DjangoPathMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["x-django-path"] = request.path
        return response


def _django_path_api() -> BoltAPI:
    api = BoltAPI(django_middleware=[f"{__name__}._DjangoPathMiddleware"])

    @api.get("/page")
    def page() -> dict:
        return {}

    return api


def test_the_django_request_has_the_script_name_of_its_own_client(settings):
    settings.FORCE_SCRIPT_NAME = "/first"
    first = TestClient(_django_path_api())
    settings.FORCE_SCRIPT_NAME = "/second"
    second = TestClient(_django_path_api())

    with first, second:
        paths = [client.get("/page").headers["x-django-path"] for client in (first, second)]

    assert paths == ["/first/page", "/second/page"]


def _list_view(request):
    return HttpResponse()


@pytest.mark.django_db
@pytest.mark.parametrize("script_name", ["/myapp", "/myapp/", "/myapp//", "/a/b", "/a+b.c~d", "/", "", None])
def test_each_script_name_gives_the_request_facts_of_django_wsgi(mount_urlconf, settings, script_name):
    api = BoltAPI(namespace="missions")

    @api.get("/{mission_id}", name="detail")
    def detail(request, mission_id: int) -> dict:
        return {**_request_facts(request, "missions:detail", mission_id=mission_id), "prefix": get_script_prefix()}

    @api.get("/list")
    def listing() -> dict:
        return {}

    settings.FORCE_SCRIPT_NAME = script_name
    settings.APPEND_SLASH = True
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
    mount_urlconf(
        [
            path("missions/", api.urls),
            path("plain/<int:mission_id>", _django_facts_view, name="plain"),
            path("plain/list/", _list_view),
        ]
    )

    with TestClient(api, follow_redirects=False) as client:
        bolt = client.get("/missions/7?x=1").json()
        # Bolt redirects to the path without the slash, Django to the path with it.
        bolt_location = client.get("/missions/list/").headers["location"]
    status, _headers, body = _django_wsgi_response("/plain/7", "x=1")
    django = {
        **json.loads(body),
        "prefix": "/" if script_name is None else script_name if script_name.endswith("/") else script_name + "/",
    }
    django_location = _django_wsgi_response("/plain/list")[1]["location"]

    assert status == 200
    assert bolt == {key: value.replace("plain", "missions") for key, value in django.items()}
    assert bolt_location + "/" == django_location.replace("plain", "missions")


@pytest.mark.parametrize("script_name", ["/my app", "/caf\u00e9", "/a%20b", "/a?b", "/a;b", 1, b"/myapp"])
def test_a_script_name_that_is_not_a_plain_path_is_refused_at_startup(settings, script_name):
    # Django percent-encodes these characters in a URL. Bolt puts the script name in paths as it is.
    settings.FORCE_SCRIPT_NAME = script_name

    with pytest.raises(ImproperlyConfigured, match="FORCE_SCRIPT_NAME"):
        TestClient(BoltAPI())


@pytest.mark.parametrize("setting", ["STATIC_URL", "MEDIA_URL"])
def test_files_at_the_script_name_are_refused_at_startup(settings, tmp_path, setting):
    # The proxy removes /files, so Bolt would serve the files at "/", and that hides each route.
    settings.FORCE_SCRIPT_NAME = "/files"
    settings.STATIC_ROOT = settings.MEDIA_ROOT = str(tmp_path)
    setattr(settings, setting, "/files/")

    with pytest.raises(ImproperlyConfigured, match=f'{setting} "/files/" is FORCE_SCRIPT_NAME'):
        TestClient(BoltAPI())


def test_the_default_media_url_at_the_script_name_serves_no_media(settings):
    # Django gives the default MEDIA_URL "" the script prefix. With no MEDIA_ROOT, Bolt serves no media.
    settings.FORCE_SCRIPT_NAME = "/myapp"
    settings.MEDIA_URL = ""
    settings.MEDIA_ROOT = ""
    api = BoltAPI()

    @api.get("/page")
    async def page() -> dict:
        return {}

    with TestClient(api) as client:
        assert client.get("/page").json() == {}


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
    root = BoltAPI(openapi_config=OpenAPIConfig(title="t", version="1", servers=[Server(url="/")]))

    for each in (api, custom, root):

        @each.get("/items/{item_id}")
        async def item(item_id: int) -> dict:
            return {"id": item_id}

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        schema = client.get("/docs/openapi.json").json()
        page = client.get("/docs/rapidoc").text
    with TestClient(custom) as client:
        # A server that the user sets stays, also the root of the host.
        custom_servers = client.get("/docs/openapi.json").json()["servers"]
    with TestClient(root) as client:
        root_servers = client.get("/docs/openapi.json").json()["servers"]

    assert schema["servers"] == [{"url": "/myapp"}]
    assert list(schema["paths"]) == ["/items/{item_id}"]
    assert 'spec-url="/myapp/docs/openapi.json"' in page
    assert custom_servers == [{"url": "https://api.example.com"}]
    assert root_servers == [{"url": "/"}]


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
    (tmp_path / "page.html").write_text("<script>alert(1)</script>")
    settings.FORCE_SCRIPT_NAME = "/myapp"
    settings.MEDIA_URL = "/myapp/media/"
    settings.MEDIA_ROOT = str(tmp_path)

    with TestClient(BoltAPI()) as client:
        assert client.get("/media/photo.txt").text == "photo"
        assert client.get("/myapp/media/photo.txt").status_code == 404
        page = client.get("/media/page.html")

    # A media file that a browser can run as script stays a download.
    assert (page.headers["content-type"], page.headers["content-disposition"]) == (
        "application/octet-stream",
        "attachment",
    )


@pytest.mark.parametrize("script_name", ["/myapp", "/myapp/", "/myapp//"])
def test_files_under_the_script_prefix_serve_at_the_path_that_the_proxy_gives(settings, tmp_path, script_name):
    # runbolt gets these values from Django for STATIC_URL "static/" and MEDIA_URL "media/".
    (tmp_path / "site.css").write_text("body {}")
    prefix = script_name if script_name.endswith("/") else script_name + "/"
    settings.FORCE_SCRIPT_NAME = script_name
    settings.STATIC_URL = prefix + "static/"
    settings.MEDIA_URL = prefix + "media/"
    settings.STATIC_ROOT = settings.MEDIA_ROOT = str(tmp_path)

    with TestClient(BoltAPI()) as client:
        statuses = [client.get(path).status_code for path in ("/static/site.css", "/media/site.css")]

    assert statuses == [200, 200]


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


async def _http_scope_app(scope, receive, send):
    body = json.dumps({"root_path": scope["root_path"], "path": scope["path"], "prefix": _prefix()}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def _websocket_scope_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    facts = {"root_path": scope["root_path"], "path": scope["path"], "prefix": _prefix()}
    await send({"type": "websocket.send", "text": json.dumps(facts)})
    await send({"type": "websocket.close"})


def test_an_asgi_app_gets_the_script_name_in_its_root_path(settings):
    # An ASGI app builds its URLs from root_path, as a WSGI app does from SCRIPT_NAME.
    api = BoltAPI()
    api.mount_asgi("/asgi", _http_scope_app)
    root = BoltAPI()
    root.mount_asgi("/", _http_scope_app)

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        mounted = client.get("/asgi/page").json()
    with TestClient(root) as client:
        at_root = client.get("/page").json()
    settings.FORCE_SCRIPT_NAME = None
    with TestClient(api) as client:
        without_setting = client.get("/asgi/page").json()

    # reverse() in the app gives the URLs of the Django project, under the script name.
    assert mounted == {"root_path": "/myapp/asgi", "path": "/page", "prefix": "/myapp/"}
    assert at_root == {"root_path": "/myapp", "path": "/page", "prefix": "/myapp/"}
    assert without_setting == {"root_path": "/asgi", "path": "/page", "prefix": "/"}


@pytest.mark.asyncio
async def test_a_websocket_asgi_app_gets_the_script_name_in_its_root_path_and_path(settings):
    # As the ASGI specification asks, the path starts with root_path.
    api = BoltAPI()
    api.mount_asgi("/ws", _websocket_scope_app)

    settings.FORCE_SCRIPT_NAME = "/myapp"
    async with WebSocketTestClient(api, "/ws/room") as ws:
        scope = json.loads(await ws.receive_text())

    assert scope == {"root_path": "/myapp/ws", "path": "/myapp/ws/room", "prefix": "/myapp/"}
    assert _prefix() == "/"


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


def _path_info_view(request):
    return JsonResponse({"path": request.path, "path_info": request.path_info})


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("script_name", "mount", "request_path", "expected"),
    [
        # The client asks for /app/app/items, and the proxy removes /app.
        ("/app", "/", "/app/items", {"path": "/app/app/items", "path_info": "/app/items"}),
        # Without a script name, a path that repeats the mount path keeps it.
        (None, "/legacy", "/legacy/legacy/items", {"path": "/legacy/legacy/items", "path_info": "/legacy/items"}),
    ],
    ids=["script-name", "mount-path"],
)
def test_a_django_mount_keeps_a_path_that_repeats_its_root_path(
    mount_urlconf, settings, script_name, mount, request_path, expected
):
    settings.FORCE_SCRIPT_NAME = script_name
    mount_urlconf([path("app/items", _path_info_view), path("legacy/items", _path_info_view)])
    api = BoltAPI()
    api.mount_django(mount)

    with TestClient(api) as client:
        assert client.get(request_path).json() == expected


@pytest.mark.django_db
@pytest.mark.parametrize("clear_root_path", [False, True])
def test_under_the_setting_a_django_mount_routes_the_path_with_the_mount_path(mount_urlconf, settings, clear_root_path):
    # Django's ASGIHandler takes FORCE_SCRIPT_NAME as the script name and ignores root_path.
    settings.FORCE_SCRIPT_NAME = "/site"
    mount_urlconf([path("legacy/page", _path_info_view), path("other", _path_info_view)])
    api = BoltAPI()
    api.mount_django("/legacy", clear_root_path=clear_root_path)

    with TestClient(api) as client:
        page = client.get("/legacy/page")
        # A URL pattern without the mount path does not match.
        other = client.get("/legacy/other")

    assert page.json() == {"path": "/site/legacy/page", "path_info": "/legacy/page"}
    assert other.status_code == 404


def _hand_written_redirect(request):
    return HttpResponse(status=302, headers={"Location": "/other/"})


def _redirect_inside(request):
    return HttpResponseRedirect(reverse("inside"))


def _redirect_outside(request):
    return HttpResponseRedirect(reverse("outside"))


@pytest.mark.django_db
@pytest.mark.parametrize("clear_root_path", [False, True])
def test_a_django_mount_redirects_as_django_wsgi_under_the_setting(mount_urlconf, settings, clear_root_path):
    # With FORCE_SCRIPT_NAME, Django ignores root_path. The URL patterns have the mount path.
    settings.FORCE_SCRIPT_NAME = "/site"
    settings.APPEND_SLASH = True
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
    mount_urlconf(
        [
            path("legacy/inside/", _slash_view, name="inside"),
            path("legacy/to-inside", _redirect_inside),
            path("legacy/to-outside", _redirect_outside),
            path("legacy/hand", _hand_written_redirect),
            path("accounts/login/", _slash_view, name="outside"),
        ]
    )
    api = BoltAPI()
    api.mount_django("/legacy", clear_root_path=clear_root_path)
    paths = ["/legacy/inside", "/legacy/to-inside", "/legacy/to-outside", "/legacy/hand"]

    with TestClient(api, follow_redirects=False) as client:
        through_bolt = {path: client.get(path).headers["location"] for path in paths}
    django = {path: _django_wsgi_response(path)[1]["location"] for path in paths}

    assert (
        through_bolt
        == django
        == {
            "/legacy/inside": "/site/legacy/inside/",
            "/legacy/to-inside": "/site/legacy/inside/",
            "/legacy/to-outside": "/site/accounts/login/",
            "/legacy/hand": "/other/",
        }
    )


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


class _InstanceNext:
    """A body whose ``__next__`` is an attribute of the instance, not of the type."""

    def __init__(self):
        chunks = [b"a", b"b"]

        def step() -> bytes:
            if not chunks:
                raise StopIteration
            return chunks.pop(0)

        self.__next__ = step


def test_a_body_with_a_next_of_its_own_streams_with_the_script_prefix(settings):
    # Rust calls the __next__ attribute, so the body with the script prefix does too.
    api = BoltAPI()

    @api.get("/stream")
    def stream():
        return StreamingResponse(_InstanceNext())

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert client.get("/stream").content == b"ab"


class _PrefixIterable:
    """An iterable that reads the script prefix in ``__iter__``."""

    def __iter__(self):
        return iter([_prefix().encode()])


class _ReplacedNext:
    """A body whose first ``__next__`` puts an end in place of itself, as Rust reads the attribute on each step."""

    def __init__(self):
        self.calls = 0

        def first() -> bytes:
            self.calls += 1
            if self.calls > 3:
                raise StopIteration
            self.__next__ = self._end
            return b"a"

        self.__next__ = first

    def _end(self) -> bytes:
        raise StopIteration


@pytest.mark.parametrize(("body", "expected"), [(_PrefixIterable, b"/myapp/"), (_ReplacedNext, b"a")])
def test_the_body_with_the_script_prefix_steps_as_rust_does(settings, body, expected):
    api = BoltAPI()

    @api.get("/stream")
    def stream():
        return StreamingResponse(body())

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        assert client.get("/stream").content == expected


class _WithBytes:
    """A chunk with ``__bytes__``: Rust sends its bytes, not its text."""

    def __bytes__(self) -> bytes:
        return b"c"

    def __str__(self) -> str:
        return "not this"


def _chunks() -> list:
    return [
        reverse_lazy("page", kwargs={"n": 1}),
        "|" + reverse("page", kwargs={"n": 2}),
        "|",
        bytearray(b"a"),
        memoryview(b"b"),
        _WithBytes(),
        3,
    ]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_lazy_chunk_of_a_body_has_the_script_prefix(mount_urlconf, settings, is_async):
    # Rust turns a chunk that is not bytes or str into text after the step of the body.
    mount_urlconf([path("page/<int:n>", _list_view, name="page")])
    api = BoltAPI()

    def sync_body():
        yield from _chunks()

    async def async_body():
        for chunk in _chunks():
            yield chunk

    @api.get("/stream")
    def stream():
        return StreamingResponse(async_body() if is_async else sync_body())

    bodies = []
    for script_name in ("/myapp", None):
        settings.FORCE_SCRIPT_NAME = script_name
        with TestClient(api) as client:
            bodies.append(client.get("/stream").content)

    assert bodies == [b"/myapp/page/1|/myapp/page/2|abc3", b"/page/1|/page/2|abc3"]


# The names of the parameters of the functions that give a hop the script prefix.
# sync_to_thread() has a parameter fn of its own, also without the setting.
_KEYWORDS = ("script_prefix", "self", "context", "func", "args", "kwargs")


def _keywords(**kwargs) -> dict:
    return kwargs


def test_a_hop_gives_each_keyword_argument_to_the_function(settings):
    api = BoltAPI()

    @api.get("/hop")
    async def hop() -> dict:
        return await sync_to_thread(_keywords, **{name: name for name in _KEYWORDS})

    results = []
    for script_name in ("/myapp", None):
        settings.FORCE_SCRIPT_NAME = script_name
        with TestClient(api, raise_server_exceptions=False) as client:
            results.append(client.get("/hop").json())

    assert results == [{name: name for name in _KEYWORDS}] * 2


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


def _ws_request_path(request: Request) -> str:
    return request["path"]


@pytest.mark.asyncio
async def test_a_websocket_has_the_paths_of_an_http_request(settings):
    api = BoltAPI()

    @api.websocket("/rooms/{room}")
    async def room(websocket: WebSocket, room: str, request_path: Annotated[str, Depends(_ws_request_path)]):
        await websocket.accept()
        await websocket.send_json(
            {"path": websocket.path, "path_info": websocket.path_info, "url": websocket.url, "dependency": request_path}
        )
        await websocket.close()

    @api.get("/rooms/{room}")
    async def page(request: Request, room: str) -> dict:
        return {"path": request.path, "path_info": request.path_info}

    facts = {}
    for script_name in ("/myapp", None):
        settings.FORCE_SCRIPT_NAME = script_name
        async with WebSocketTestClient(api, "/rooms/a%20b") as ws:
            facts[script_name] = await ws.receive_json()
        with TestClient(api) as client:
            http = client.get("/rooms/a%20b").json()
        # A WebSocket has the same path and path info as an HTTP request to the same path.
        assert {"path": facts[script_name]["path"], "path_info": facts[script_name]["path_info"]} == http

    assert facts == {
        "/myapp": {
            "path": "/myapp/rooms/a%20b",
            "path_info": "/rooms/a%20b",
            "url": "/myapp/rooms/a%20b",
            "dependency": "/myapp/rooms/a%20b",
        },
        None: {
            "path": "/rooms/a%20b",
            "path_info": "/rooms/a%20b",
            "url": "/rooms/a%20b",
            "dependency": "/rooms/a%20b",
        },
    }


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


# --- The state of a thread ---------------------------------------------------------
#
# Django keeps thread state in asgiref Locals: cache backends, the active
# language and time zone. The setting must not change what that state does.

_BACKENDS: list[str] = []


class _CountingCache(LocMemCache):
    """A cache that counts its backends. A Redis backend opens a connection pool in each."""

    def __init__(self, name, params):
        _BACKENDS.append(name)
        super().__init__(name, params)


def _cached(*_args) -> object:
    return caches["default"].get("key")


class _CacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _cached()
        return self.get_response(request)


def _cache_api(config: str) -> BoltAPI:
    def formatter(error, request) -> JSON:
        _cached()
        return JSON({}, status_code=error.status)

    kwargs = {
        "plain": {},
        "django_middleware": {"django_middleware": [f"{__name__}._CacheMiddleware"]},
        "error_formatter": {"error_formatter": formatter},
    }[config]
    api = BoltAPI(**kwargs)

    @api.get("/cached")
    def cached() -> dict:
        _cached()
        return {}

    return api


@pytest.mark.parametrize(
    ("config", "route"), [("plain", "/cached"), ("django_middleware", "/cached"), ("error_formatter", "/missing")]
)
def test_a_sync_request_keeps_the_cache_backend_of_its_thread(settings, config, route):
    made = {}
    for script_name in ("/myapp", None):
        settings.FORCE_SCRIPT_NAME = script_name
        # New CACHES settings give the cache handler new Locals, so no backend exists yet.
        settings.CACHES = {"default": {"BACKEND": f"{__name__}._CountingCache", "LOCATION": config}}
        _BACKENDS.clear()
        with TestClient(_cache_api(config)) as client:
            for _ in range(20):
                client.get(route)
        made[script_name] = len(_BACKENDS)

    # A request lane gives each request a context of its own, so a backend each.
    assert made == ({"/myapp": 20, None: 20} if config == "django_middleware" else {"/myapp": 1, None: 1})


_HANDLER_VARIABLE: contextvars.ContextVar[str] = contextvars.ContextVar("handler_variable", default="unset")


def test_a_context_variable_of_a_sync_handler_stays_on_its_thread_as_without_the_setting(settings):
    api = BoltAPI()

    @api.get("/set/{value}")
    def set_value(value: str) -> dict:
        before = _HANDLER_VARIABLE.get()
        _HANDLER_VARIABLE.set(value)
        return {"before": before}

    seen = {}
    for script_name, values in (("/myapp", ("a1", "b1", "c1")), (None, ("a2", "b2", "c2"))):
        settings.FORCE_SCRIPT_NAME = script_name
        with TestClient(api) as client:
            # The requests of one test thread go to one worker thread, as under WSGI.
            seen[script_name] = [client.get(f"/set/{value}").json()["before"] for value in values][1:]

    assert seen == {"/myapp": ["a1", "b1"], None: ["a2", "b2"]}


class _LanguageMiddleware(MiddlewareMixin):
    """Set state on the request path and read it on the response path, as LocaleMiddleware does."""

    def process_request(self, request):
        _HANDLER_VARIABLE.set("middleware")
        translation.activate("fr")

    def process_response(self, request, response):
        response["x-state"] = f"{_HANDLER_VARIABLE.get()} {translation.get_language()}"
        translation.deactivate()
        return response


@pytest.mark.parametrize("handler_kind", ["sync", "async"])
def test_sync_middleware_keeps_its_state_from_the_request_to_the_response(settings, handler_kind):
    api = BoltAPI(django_middleware=[f"{__name__}._LanguageMiddleware"])

    def state() -> dict:
        return {"state": f"{_HANDLER_VARIABLE.get()} {translation.get_language()}"}

    if handler_kind == "sync":
        api.get("/state")(state)
    else:

        @api.get("/state")
        async def async_state() -> dict:
            return state()

    settings.FORCE_SCRIPT_NAME = "/myapp"
    with TestClient(api) as client:
        response = client.get("/state")

    assert (response.json()["state"], response.headers["x-state"]) == ("middleware fr", "middleware fr")


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
    "/missing",
]


def _hammer(api: BoltAPI, requests: int, concurrency: int) -> set[str]:
    """Send ``requests`` requests from ``concurrency`` threads over the routes. Return every prefix that they saw."""
    seen: set[str] = set()
    lock = threading.Lock()
    routes = itertools.cycle(_LEAK_ROUTES)
    with TestClient(api, raise_server_exceptions=False) as client:

        def one(route: str) -> None:
            response = client.get(route)
            found = _prefixes(response.json())
            # The middleware of the client, if it has one, sends what it saw.
            found.update(value for name, value in response.headers.items() if name in ("x-before", "x-after"))
            with lock:
                seen.update(found)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, [next(routes) for _ in range(requests)]))
    return seen


@pytest.mark.django_db(transaction=True)
def test_the_script_prefix_of_one_client_never_reaches_another(settings):
    User.objects.create(username="row")
    configs = {
        "plain": {"error_formatter": _formatter},
        "python": {"middleware": [_PyMiddleware], "error_formatter": _formatter},
        "django": {"django_middleware": [f"{__name__}._DjangoMiddleware"], "error_formatter": _formatter},
    }
    # Each client with a script name comes between two without one, on each kind of thread.
    order = [("/myapp", "plain"), (None, "plain"), ("/myapp", "python"), (None, "django"), ("/other", "django")]
    order += [(None, "python"), ("/other", "plain"), (None, "django")]
    rounds = []
    for _ in range(3):
        for script_name, config in order:
            settings.FORCE_SCRIPT_NAME = script_name
            rounds.append((script_name, _hammer(_build(**configs[config]), requests=700, concurrency=200)))

    assert rounds == [
        (script_name, {"/" if script_name is None else f"{script_name}/"}) for script_name, _seen_set in rounds
    ]
    assert get_script_prefix() == "/"
