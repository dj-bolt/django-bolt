"""Code outside the handler body gets the request parts that it reads.

Rust parses only the request parts that a route needs. The analysis finds
them in the source of the handler and of its dependencies. Each test sends
a part and checks that the code gets it, not an empty value. A blocking
handler must run in the thread pool, not on the server thread.
"""

from __future__ import annotations

import functools
import threading
import urllib.request
from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.analysis import analyze_handler
from django_bolt.params import Depends
from django_bolt.testing import TestClient

from .test_models import Article

# The source of a lambda is its line, so these must be at module level.
lambda_handler = lambda request: {"query": dict(request.query)}  # noqa: E731
lambda_dependency = lambda request: dict(request.query)  # noqa: E731
two_lambdas_on_a_line = [lambda request: dict(request.query), lambda request: dict(request.headers)]


def reads_the_token_header(fn):
    """A decorator that reads a header before the dependency runs."""

    @functools.wraps(fn)
    def wrapper(request):
        return {"token": request.headers.get("x-token"), **fn(request)}

    return wrapper


@reads_the_token_header
def decorated_dependency(request):
    return {}


class TokenInInit:
    """A call of the class runs __init__, not the first method."""

    def describe(self):
        return "a method before __init__"

    def __init__(self, request):
        self.token = request.headers.get("x-token")


@dataclass
class RequestHolder:
    """The dataclass makes __init__ at run time, so __init__ has no source."""

    request: Any


def reads_the_token_header_of_a_method(fn):
    @functools.wraps(fn)
    def wrapper(self, request):
        return {"token": request.headers.get("x-token"), **fn(self, request)}

    return wrapper


class TokenReader:
    """A callable dependency. The decorator of __call__ reads the header."""

    @reads_the_token_header_of_a_method
    def __call__(self, request):
        return {}


def sets_the_token_from_the_header(fn):
    """A decorator of __init__ that reads a header before __init__ runs."""

    @functools.wraps(fn)
    def wrapper(self, request):
        self.token = request.headers.get("x-token")
        fn(self, request)

    return wrapper


class TokenInDecoratedInit:
    """The decorator of __init__ reads the header, not the body of __init__."""

    @sets_the_token_from_the_header
    def __init__(self, request):
        pass


class InheritedTokenInInit(TokenInInit):
    """The class inherits __init__. Its source has only the __init__ of a nested class."""

    class Query:
        def __init__(self, request):
            self.query = dict(request.query)


def _request_parts(api: BoltAPI, path: str) -> tuple[str, ...]:
    """Return the request parts that Rust parses for the route."""
    meta = next(api._handler_middleware[hid] for _method, route, hid, _fn in api._routes if route == path)
    return tuple(part for part in ("body", "query", "headers", "cookies") if meta[f"needs_{part}"])


def test_a_lambda_handler_gets_the_query():
    api = BoltAPI()
    api.get("/search")(lambda_handler)

    with TestClient(api) as client:
        response = client.get("/search?q=bolt")

    assert response.json() == {"query": {"q": "bolt"}}


def test_a_lambda_dependency_gets_the_query():
    api = BoltAPI()

    @api.get("/search")
    def search(query: Annotated[dict, Depends(lambda_dependency)]):
        return {"query": query}

    with TestClient(api) as client:
        response = client.get("/search?q=bolt")

    assert response.json() == {"query": {"q": "bolt"}}


def test_a_lambda_dependency_on_a_line_with_another_lambda_gets_the_query():
    """The analysis cannot tell which lambda it is, so the route parses every part."""
    api = BoltAPI()

    @api.get("/search")
    def search(query: Annotated[dict, Depends(two_lambdas_on_a_line[0])]):
        return {"query": query}

    with TestClient(api) as client:
        response = client.get("/search?q=bolt")

    assert response.json() == {"query": {"q": "bolt"}}


def test_a_decorated_dependency_gets_the_header_that_its_decorator_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(token: Annotated[dict, Depends(decorated_dependency)]):
        return token

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


def test_a_class_dependency_gets_the_header_that_its_init_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[TokenInInit, Depends(TokenInInit)]):
        return {"token": holder.token}

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


def test_a_class_dependency_gets_the_header_that_the_decorator_of_its_init_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[TokenInDecoratedInit, Depends(TokenInDecoratedInit)]):
        return {"token": holder.token}

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


def test_a_class_dependency_gets_the_header_that_its_inherited_init_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[InheritedTokenInInit, Depends(InheritedTokenInInit)]):
        return {"token": holder.token}

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


def test_a_class_dependency_whose_init_has_no_source_gets_every_part():
    """The analysis cannot read the __init__, so the route parses every part."""
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[RequestHolder, Depends(RequestHolder)]):
        return {"token": holder.request.headers.get("x-token")}

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


@pytest.mark.django_db(transaction=True)
def test_a_sync_viewset_action_that_uses_the_orm_runs_in_the_thread_pool():
    """The analysis must read the action method, not the generic wrapper."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        @action(methods=["GET"], detail=False)
        def count(self, request, limit: int = 5):
            return {"count": Article.objects.count(), "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/articles/count")

    assert response.json()["count"] == 0
    assert response.json()["thread"].startswith("bolt_default")


def test_a_sync_handler_that_calls_urlopen_runs_in_the_thread_pool():
    api = BoltAPI()

    @api.get("/fetch")
    def fetch(url: str = ""):
        if url:
            return urllib.request.urlopen(url).read()
        return {"thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/fetch")

    assert response.json()["thread"].startswith("bolt_default")


def test_a_callable_dependency_gets_the_header_that_the_decorator_of_its_call_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(token: Annotated[dict, Depends(TokenReader())]):
        return token

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"token": "secret"}


def test_a_lambda_dependency_makes_its_route_parse_only_what_it_reads():
    api = BoltAPI()

    @api.get("/search")
    def search(query: Annotated[dict, Depends(lambda_dependency)]):
        return query

    assert _request_parts(api, "/search") == ("query",)


def test_a_class_dependency_makes_its_route_parse_only_what_its_init_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[TokenInInit, Depends(TokenInInit)]):
        return {"token": holder.token}

    assert _request_parts(api, "/items") == ("headers",)


def test_a_class_dependency_makes_its_route_parse_only_what_its_inherited_init_reads():
    api = BoltAPI()

    @api.get("/items")
    def items(holder: Annotated[InheritedTokenInInit, Depends(InheritedTokenInInit)]):
        return {"token": holder.token}

    assert _request_parts(api, "/items") == ("headers",)


def test_a_line_with_two_lambdas_makes_the_route_parse_every_part():
    api = BoltAPI()

    @api.get("/search")
    def search(query: Annotated[dict, Depends(two_lambdas_on_a_line[0])]):
        return query

    assert _request_parts(api, "/search") == ("body", "query", "headers", "cookies")


def test_the_failed_analysis_names_the_lambda_that_it_did_not_find():
    analysis = analyze_handler(two_lambdas_on_a_line[0], {"request"})

    assert analysis.analysis_failed is True
    assert analysis.failure_reason == "Could not find one definition of <lambda> in its source"


def test_the_analysis_fails_for_a_class_whose_init_has_no_source():
    """A dataclass makes its __init__ at run time, so the __init__ has no source."""
    analysis = analyze_handler(RequestHolder, {"request"})

    assert analysis.analysis_failed is True
    assert analysis.failure_reason.startswith("Could not get source:")
