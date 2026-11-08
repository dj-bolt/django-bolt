"""The compiled injector passes each parameter as the handler declares it.

A parameter can be positional-or-keyword, positional-only (before ``/``) or
keyword-only (after ``*``). Each injector pattern must pass the value by the
right convention. A constrained type (``msgspec.Meta``) keeps Rust from
binding the value, so the Python injector runs. Each test registers its
routes, so a mutation tool sees the test reach the injector.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI, Depends, UploadFile
from django_bolt.param_functions import Cookie, File, Form, Header
from django_bolt.testing import TestClient

Count = Annotated[int, msgspec.Meta(ge=0)]


class Item(msgspec.Struct):
    name: str


class Profile(msgspec.Struct):
    name: str
    avatar: UploadFile


UPLOADS: list[UploadFile] = []


def _sync_dep() -> int:
    return 10


async def _async_dep() -> int:
    return 20


async def _other_async_dep() -> int:
    return 30


@pytest.fixture
def client():
    api = BoltAPI()

    # Path parameters only.
    @api.get("/path-kw/{a}/{b}")
    async def path_kw(*, a: Count, b: Count):
        return {"a": a, "b": b}

    @api.get("/path-pos/{a}/{b}")
    async def path_pos(a: Count, /, b: Count):
        return {"a": a, "b": b}

    # Query parameters only.
    @api.get("/query-kw")
    async def query_kw(*, a: Count, b: Count = 7):
        return {"a": a, "b": b}

    @api.get("/query-pos")
    async def query_pos(a: Count, /, b: Count = 7):
        return {"a": a, "b": b}

    # Path and query parameters.
    @api.get("/simple-kw/{a}")
    async def simple_kw(*, a: Count, b: Count = 7):
        return {"a": a, "b": b}

    @api.get("/simple-pos/{a}")
    async def simple_pos(a: Count, /, b: Count = 7):
        return {"a": a, "b": b}

    # A body only.
    @api.post("/body-kw")
    async def body_kw(*, item: Item):
        return {"name": item.name}

    @api.post("/body-pos")
    async def body_pos(item: Item, /):
        return {"name": item.name}

    # A body and a query parameter.
    @api.post("/body-query-kw")
    async def body_query_kw(*, item: Item, a: Count = 7):
        return {"name": item.name, "a": a}

    @api.post("/body-query-pos")
    async def body_query_pos(item: Item, a: Count = 7, /):
        return {"name": item.name, "a": a}

    # A header and a cookie.
    @api.get("/full-kw")
    async def full_kw(*, x_a: Annotated[Count, Header()], b: Annotated[Count, Cookie()] = 7):
        return {"a": x_a, "b": b}

    @api.get("/full-pos")
    async def full_pos(x_a: Annotated[Count, Header()], b: Annotated[Count, Cookie()] = 7, /):
        return {"a": x_a, "b": b}

    # Dependencies: sync only, one async, and several async.
    @api.get("/deps-sync-kw")
    async def deps_sync_kw(*, a: Count = 1, dep: int = Depends(_sync_dep)):
        return {"a": a, "dep": dep}

    @api.get("/deps-sync-pos")
    async def deps_sync_pos(a: Count = 1, dep: int = Depends(_sync_dep), /):
        return {"a": a, "dep": dep}

    @api.get("/deps-async-kw")
    async def deps_async_kw(*, a: Count = 1, dep: int = Depends(_async_dep)):
        return {"a": a, "dep": dep}

    @api.get("/deps-async-pos")
    async def deps_async_pos(a: Count = 1, dep: int = Depends(_async_dep), /):
        return {"a": a, "dep": dep}

    @api.get("/deps-parallel-kw")
    async def deps_parallel_kw(*, first: int = Depends(_async_dep), second: int = Depends(_other_async_dep)):
        return {"first": first, "second": second}

    @api.get("/deps-parallel-pos")
    async def deps_parallel_pos(first: int = Depends(_async_dep), second: int = Depends(_other_async_dep), /):
        return {"first": first, "second": second}

    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_path_parameters_follow_the_declared_convention(client, kind):
    assert client.get(f"/path-{kind}/3/4").json() == {"a": 3, "b": 4}


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_query_parameters_follow_the_declared_convention(client, kind):
    assert client.get(f"/query-{kind}?a=3&b=4").json() == {"a": 3, "b": 4}
    assert client.get(f"/query-{kind}?a=3").json() == {"a": 3, "b": 7}
    assert client.get(f"/query-{kind}").status_code == 422


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_path_and_query_parameters_follow_the_declared_convention(client, kind):
    assert client.get(f"/simple-{kind}/3?b=4").json() == {"a": 3, "b": 4}
    assert client.get(f"/simple-{kind}/3").json() == {"a": 3, "b": 7}


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_a_body_follows_the_declared_convention(client, kind):
    response = client.post(f"/body-{kind}", json={"name": "bolt"})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt"}


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_a_body_and_a_query_parameter_follow_the_declared_convention(client, kind):
    response = client.post(f"/body-query-{kind}?a=3", json={"name": "bolt"})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt", "a": 3}


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_a_header_and_a_cookie_follow_the_declared_convention(client, kind):
    response = client.get(f"/full-{kind}", headers={"X-A": "3", "Cookie": "b=4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"a": 3, "b": 4}
    assert client.get(f"/full-{kind}", headers={"X-A": "3"}).json() == {"a": 3, "b": 7}


@pytest.mark.parametrize("flavour", ["sync", "async"])
@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_a_dependency_follows_the_declared_convention(client, flavour, kind):
    expected_dep = 10 if flavour == "sync" else 20
    assert client.get(f"/deps-{flavour}-{kind}?a=3").json() == {"a": 3, "dep": expected_dep}
    assert client.get(f"/deps-{flavour}-{kind}").json() == {"a": 1, "dep": expected_dep}


@pytest.mark.parametrize("kind", ["kw", "pos"])
def test_several_async_dependencies_follow_the_declared_convention(client, kind):
    assert client.get(f"/deps-parallel-{kind}").json() == {"first": 20, "second": 30}


def test_two_async_dependencies_resolve_at_the_same_time():
    """Each dependency waits for the other one to start. A sequential run would time out."""
    api = BoltAPI()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> int:
        first_started.set()
        await asyncio.wait_for(second_started.wait(), 2)
        return 1

    async def second() -> int:
        second_started.set()
        await asyncio.wait_for(first_started.wait(), 2)
        return 2

    @api.get("/together")
    async def together(x: int = Depends(first), y: int = Depends(second)):
        return {"x": x, "y": y}

    with TestClient(api) as test_client:
        response = test_client.get("/together")

    assert response.status_code == 200, response.text
    assert response.json() == {"x": 1, "y": 2}


# --- A handler with dependencies and parameters of its own ---
# The injector of such a handler reads each source by the id it has in the plan.
# One handler reads every source, so two sources with one id would show.


def _register_with_dependency(api: BoltAPI, prefix: str, dependency, *, is_async: bool) -> None:
    if is_async:

        @api.get(f"{prefix}/all/{{a}}")
        async def every_source(
            request,
            a: Count,
            b: Count,
            x_c: Annotated[Count, Header()],
            d: Annotated[Count, Cookie()],
            dep: int = Depends(dependency),
        ):
            return {"a": a, "b": b, "c": x_c, "d": d, "dep": dep, "method": request.method}

        @api.post(f"{prefix}/body")
        async def body_route(item: Item, dep: int = Depends(dependency)):
            return {"name": item.name, "dep": dep}

        @api.post(f"{prefix}/form")
        async def form_route(
            title: Annotated[str, Form()], doc: Annotated[UploadFile, File()], dep: int = Depends(dependency)
        ):
            UPLOADS.append(doc)
            return {"title": title, "filename": doc.filename, "dep": dep}

        @api.post(f"{prefix}/profile")
        async def profile_route(data: Annotated[Profile, Form()], dep: int = Depends(dependency)):
            UPLOADS.append(data.avatar)
            return {"name": data.name, "filename": data.avatar.filename, "dep": dep}

    else:

        @api.get(f"{prefix}/all/{{a}}")
        def every_source(
            request,
            a: Count,
            b: Count,
            x_c: Annotated[Count, Header()],
            d: Annotated[Count, Cookie()],
            dep: int = Depends(dependency),
        ):
            return {"a": a, "b": b, "c": x_c, "d": d, "dep": dep, "method": request.method}

        @api.post(f"{prefix}/body")
        def body_route(item: Item, dep: int = Depends(dependency)):
            return {"name": item.name, "dep": dep}

        @api.post(f"{prefix}/form")
        def form_route(
            title: Annotated[str, Form()], doc: Annotated[UploadFile, File()], dep: int = Depends(dependency)
        ):
            UPLOADS.append(doc)
            return {"title": title, "filename": doc.filename, "dep": dep}

        @api.post(f"{prefix}/profile")
        def profile_route(data: Annotated[Profile, Form()], dep: int = Depends(dependency)):
            UPLOADS.append(data.avatar)
            return {"name": data.name, "filename": data.avatar.filename, "dep": dep}


@pytest.fixture
def dependency_client():
    api = BoltAPI()
    _register_with_dependency(api, "/sync-dep-sync-handler", _sync_dep, is_async=False)
    _register_with_dependency(api, "/sync-dep-async-handler", _sync_dep, is_async=True)
    _register_with_dependency(api, "/async-dep-async-handler", _async_dep, is_async=True)
    UPLOADS.clear()
    with TestClient(api) as test_client:
        yield test_client


FLAVOURS = [
    ("/sync-dep-sync-handler", 10),
    ("/sync-dep-async-handler", 10),
    ("/async-dep-async-handler", 20),
]


@pytest.mark.parametrize(("prefix", "dep"), FLAVOURS)
def test_a_handler_with_a_dependency_reads_every_source(dependency_client, prefix, dep):
    response = dependency_client.get(f"{prefix}/all/1?b=2", headers={"X-C": "3", "Cookie": "d=4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"a": 1, "b": 2, "c": 3, "d": 4, "dep": dep, "method": "GET"}


@pytest.mark.parametrize(("prefix", "dep"), FLAVOURS)
def test_a_handler_with_a_dependency_answers_a_missing_value_with_a_422(dependency_client, prefix, dep):
    response = dependency_client.get(f"{prefix}/all/1?b=2", headers={"X-C": "3"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Missing required cookie: d"


@pytest.mark.parametrize(("prefix", "dep"), FLAVOURS)
def test_a_handler_with_a_dependency_reads_the_body(dependency_client, prefix, dep):
    response = dependency_client.post(f"{prefix}/body", json={"name": "bolt"})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt", "dep": dep}


@pytest.mark.parametrize(("prefix", "dep"), FLAVOURS)
def test_a_handler_with_a_dependency_reads_form_fields_and_files(dependency_client, prefix, dep):
    response = dependency_client.post(
        f"{prefix}/form", data={"title": "t"}, files={"doc": ("a.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"title": "t", "filename": "a.txt", "dep": dep}
    assert UPLOADS[-1]._file.closed


@pytest.mark.parametrize(("prefix", "dep"), FLAVOURS)
def test_a_handler_with_a_dependency_reads_a_form_struct_with_a_file(dependency_client, prefix, dep):
    response = dependency_client.post(
        f"{prefix}/profile", data={"name": "n"}, files={"avatar": ("p.png", b"png", "image/png")}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "n", "filename": "p.png", "dep": dep}
    assert UPLOADS[-1]._file.closed
