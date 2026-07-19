"""Micro-benchmarks for the Python request hot path.

These isolate the per-request Python work — argument injection, dependency
argument extraction, and response serialization — with synthetic request
dicts, so a change that is only 1-3% end-to-end is directly measurable.

Requires pytest-benchmark (kept out of the default test run — this directory
is not collected by `just test-py`). Run with `just bench-micro`.
"""

from __future__ import annotations

import asyncio

import msgspec
import pytest

pytest.importorskip("pytest_benchmark")

from django_bolt import _json  # noqa: E402
from django_bolt._kwargs.model import compile_argument_injector, compile_binder  # noqa: E402
from django_bolt.dependencies import _extract_dependency_args  # noqa: E402
from django_bolt.serialization import (  # noqa: E402
    compile_response_handlers,
    serialize_response_sync,
)


def _compile(fn, method: str, path: str):
    meta = compile_binder(fn, method, path)
    meta["injector"] = compile_argument_injector(meta, {}, compile_binder)
    return meta


# ── Injectors ────────────────────────────────────────────────────────────────


def test_injector_path_only(benchmark):
    async def handler(item_id: int):
        return {"id": item_id}

    meta = _compile(handler, "GET", "/items/{item_id}")
    injector = meta["injector"]
    request = {"params": {"item_id": 42}}

    result = benchmark(injector, request)
    assert result == ([42], {})


def test_injector_query_only(benchmark):
    async def handler(q: str, page: int = 1):
        return {"q": q, "page": page}

    meta = _compile(handler, "GET", "/search")
    injector = meta["injector"]
    request = {"query": {"q": "hello", "page": 3}}

    result = benchmark(injector, request)
    assert result == (["hello", 3], {})


def test_injector_simple_path_and_query(benchmark):
    async def handler(item_id: int, q: str, limit: int = 10):
        return {}

    meta = _compile(handler, "GET", "/items/{item_id}")
    injector = meta["injector"]
    request = {"params": {"item_id": 7}, "query": {"q": "x", "limit": 5}}

    result = benchmark(injector, request)
    assert result == ([7, "x", 5], {})


# ── Dependency argument extraction ───────────────────────────────────────────


def test_dependency_arg_extraction(benchmark):
    def dep(item_id: int, q: str):
        return (item_id, q)

    dep_meta = compile_binder(dep, "GET", "/items/{item_id}")
    params_map = {"item_id": 3}
    query_map = {"q": "hello"}

    args, kwargs = benchmark(_extract_dependency_args, dep_meta, {}, params_map, query_map, {}, {})
    assert args == [3, "hello"]
    assert kwargs == {}


# ── Response serialization ───────────────────────────────────────────────────


class Item(msgspec.Struct):
    id: int
    name: str
    price: float


def _response_meta(response_type=None, status=200):
    meta = {
        "default_status_code": status,
        "response_type": response_type,
        "validate_response": True,
    }
    compile_response_handlers(meta)
    return meta


def test_serialize_dict_response(benchmark):
    meta = _response_meta()
    payload = {"message": "hello", "count": 3, "items": [1, 2, 3]}

    wire = benchmark(serialize_response_sync, payload, meta)
    assert wire[0] == 200


def test_serialize_struct_response_validated(benchmark):
    meta = _response_meta(response_type=Item)
    payload = Item(id=1, name="widget", price=9.99)

    wire = benchmark(serialize_response_sync, payload, meta)
    assert wire[0] == 200


def test_serialize_list_of_structs(benchmark):
    meta = _response_meta(response_type=list[Item])
    payload = [Item(id=i, name=f"item-{i}", price=float(i)) for i in range(50)]

    wire = benchmark(serialize_response_sync, payload, meta)
    assert wire[0] == 200


def test_json_encode_10k_payload(benchmark):
    payload = {"items": [{"id": i, "name": f"item-{i}", "tags": ["a", "b"]} for i in range(100)]}
    body = benchmark(_json.encode, payload)
    assert len(body) > 1000


# ── Full executor round trip (async, no sockets) ─────────────────────────────


def test_handler_executor_round_trip(benchmark):
    """Executor-level benchmark: injector + handler call + serialization."""
    from django_bolt import BoltAPI  # noqa: PLC0415

    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item(item_id: int, q: str = "none"):
        return {"id": item_id, "q": q}

    handler, meta = None, None
    for _method, _path, hid, fn in api._routes:
        handler, meta = fn, api._handler_meta[hid]

    executor = meta["_handler_executor"]
    request = {"params": {"item_id": 5}, "query": {"q": "hello"}}
    loop = asyncio.new_event_loop()
    try:

        def run():
            return loop.run_until_complete(executor(handler, request))

        wire = benchmark(run)
        assert wire[0] == 200
    finally:
        loop.close()
