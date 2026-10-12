"""
Tests for static analysis module.

Tests the AST-based analysis of handler functions for:
- Django ORM usage detection
- Blocking I/O detection
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import urllib.parse
from time import sleep

import pytest
from django.contrib.auth.models import User
from django.db import connection, connections

from django_bolt import BoltAPI
from django_bolt.analysis import (
    DependencyNeeds,
    HandlerAnalysis,
    analyze_dependency_tree,
    analyze_handler,
    resolve_introspection_target,
)
from django_bolt.testing import TestClient


# Test handler functions for analysis
def sync_handler_with_orm():
    """Sync handler that uses Django ORM."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    users = User.objects.filter(is_active=True)
    return list(users)


def sync_handler_with_multiple_orm_calls():
    """Sync handler with multiple ORM operations."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    User.objects.create(username="test")
    users = User.objects.all()
    count = User.objects.count()
    first = User.objects.first()
    return {"users": list(users), "count": count, "first": first}


async def async_handler_with_sync_orm():
    """Async handler that incorrectly uses sync ORM methods."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    users = User.objects.filter(is_active=True)
    return list(users)


async def async_handler_with_async_orm():
    """Async handler using proper async ORM methods."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    user = await User.objects.aget(id=1)
    users = [u async for u in User.objects.aiterator()]
    return {"user": user, "users": users}


def sync_handler_no_orm():
    """Sync handler without any ORM calls."""
    data = {"message": "Hello, World!"}
    return data


async def async_handler_no_orm():
    """Async handler without any ORM calls."""
    return {"status": "ok"}


def sync_handler_with_iteration():
    """Sync handler that iterates over QuerySet."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    result = []
    for user in User.objects.all():
        result.append(user.username)
    return result


def sync_handler_with_list_comprehension():
    """Sync handler with list comprehension over QuerySet."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    return [user.username for user in User.objects.filter(is_active=True)]


def sync_handler_with_save():
    """Sync handler that saves a model instance."""
    from django.contrib.auth.models import User  # noqa: PLC0415

    user = User(username="test")
    user.save()
    return {"id": user.id}


def sync_handler_with_blocking_io():
    """Sync handler with blocking I/O operations."""
    import time  # noqa: PLC0415

    time.sleep(1)
    return {"waited": True}


def sync_handler_with_requests():
    """Sync handler using requests library."""
    import requests  # noqa: PLC0415 - intentional for testing blocking I/O detection

    response = requests.get("http://example.com")
    return {"status": response.status_code}


def handler_reads_request_headers(request):
    """Handler that directly reads request.headers."""
    return request.headers.get("x-test")


def handler_reads_request_body(request):
    """Handler that directly reads request.body."""
    return request.body


def handler_reads_request_query(request):
    """Handler that directly reads request.query."""
    return request.query.get("page")


def handler_reads_request_cookies(request):
    """Handler that directly reads request.cookies."""
    return request.cookies.get("session")


def handler_reads_request_meta(request):
    """Handler that reads Django-compatible META."""
    return {
        "host": request.META.get("HTTP_HOST"),
        "query": request.META.get("QUERY_STRING"),
    }


def handler_reads_request_full_path(request):
    """Handler that reads request.get_full_path()."""
    return request.get_full_path()


def handler_reads_request_absolute_uri(request):
    """Handler that reads request.build_absolute_uri()."""
    return request.build_absolute_uri()


def handler_reads_request_get_known_keys(request):
    """Handler that uses request.get(...) with known component keys."""
    return {
        "headers": request.get("headers"),
        "query": request.get("query"),
        "body": request.get("body"),
        "cookies": request.get("cookies"),
    }


def handler_reads_request_subscript_known_keys(request):
    """Handler that uses request[...] with known component keys."""
    return {
        "headers": request["headers"],
        "query": request["query"],
        "body": request["body"],
        "cookies": request["cookies"],
    }


def handler_reads_request_in_local_helper(request):
    """Handler that reads request components inside a nested helper."""

    def inner():
        return request.headers.get("x-test"), request.query.get("page")

    return inner()


def handler_reads_req_alias(req):
    """Handler that uses a non-default request parameter name."""
    return req.headers.get("x-test"), req.cookies.get("session")


def handler_reads_request_unknown_keys(request):
    """Handler that reads request via unknown keys only."""
    return request.get("state"), request["state"]


def external_helper_reads_request_headers(request):
    """External helper used to document current analysis limits."""
    return request.headers.get("x-test")


def handler_passes_request_to_external_helper(request):
    """Handler that delegates request usage to an external helper."""
    return external_helper_reads_request_headers(request)


# inspect.getsource gives only the second line, which does not parse alone.
lambda_on_a_line_that_does_not_parse = functools.partial(
    print, lambda request: request.query)  # fmt: skip


class TestHandlerAnalysis:
    """Tests for analyze_handler function."""

    def test_sync_handler_with_orm_detection(self):
        """Test that ORM usage is correctly detected."""
        analysis = analyze_handler(sync_handler_with_orm)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"filter"}
        # list() is not blocking I/O.
        assert analysis.has_blocking_io is False
        assert analysis.blocking_operations == set()
        assert analysis.is_blocking is True
        assert analysis.analysis_failed is False

    def test_sync_handler_with_multiple_orm_calls(self):
        """Test detection of multiple ORM operations."""
        analysis = analyze_handler(sync_handler_with_multiple_orm_calls)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"create", "all", "count", "first"}

    def test_async_handler_with_sync_orm(self):
        """Test that ORM in async handler is detected."""
        analysis = analyze_handler(async_handler_with_sync_orm)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"filter"}

    def test_async_handler_with_async_orm(self):
        """Test that async ORM methods are correctly identified."""
        analysis = analyze_handler(async_handler_with_async_orm)

        assert analysis.uses_orm is True
        # The async comprehension iterates a QuerySet method too.
        assert analysis.orm_operations == {"aget", "aiterator", "comprehension_aiterator"}

    def test_sync_handler_no_orm(self):
        """Test that handlers without ORM are correctly identified."""
        analysis = analyze_handler(sync_handler_no_orm)

        assert analysis.uses_orm is False
        assert analysis.is_blocking is False
        assert len(analysis.orm_operations) == 0

    def test_async_handler_no_orm(self):
        """Test async handler without ORM."""
        analysis = analyze_handler(async_handler_no_orm)

        assert analysis.uses_orm is False
        assert analysis.is_blocking is False

    def test_iteration_over_queryset(self):
        """Test detection of for-loop iteration over QuerySet."""
        analysis = analyze_handler(sync_handler_with_iteration)

        assert analysis.uses_orm is True
        # result.append() is not ORM work.
        assert analysis.orm_operations == {"all", "iterate_all"}

    def test_list_comprehension_over_queryset(self):
        """Test detection of list comprehension over QuerySet."""
        analysis = analyze_handler(sync_handler_with_list_comprehension)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"filter", "comprehension_filter"}

    def test_model_save_detection(self):
        """Test detection of model.save() calls."""
        analysis = analyze_handler(sync_handler_with_save)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"save"}

    def test_blocking_io_detection(self):
        """Test detection of blocking I/O operations."""
        analysis = analyze_handler(sync_handler_with_blocking_io)

        assert analysis.has_blocking_io is True
        assert analysis.blocking_operations == {"time.sleep"}
        assert analysis.uses_orm is False
        assert analysis.is_blocking is True

    def test_requests_library_detection(self):
        """Test detection of requests library usage."""
        analysis = analyze_handler(sync_handler_with_requests)

        assert analysis.has_blocking_io is True
        assert analysis.blocking_operations == {"requests.get"}


class TestRequestComponentAnalysis:
    """Tests for AST-based request component detection."""

    @staticmethod
    def assert_request_flags(analysis, *, body=False, query=False, headers=False, cookies=False):
        """Assert the detected request-component flags."""
        assert analysis.request_needs_body is body
        assert analysis.request_needs_query is query
        assert analysis.request_needs_headers is headers
        assert analysis.request_needs_cookies is cookies

    def test_direct_request_headers_detection(self):
        """Direct request.headers access sets only the headers flag."""
        analysis = analyze_handler(handler_reads_request_headers, request_param_names={"request"})
        self.assert_request_flags(analysis, headers=True)

    def test_direct_request_body_detection(self):
        """Direct request.body access sets only the body flag."""
        analysis = analyze_handler(handler_reads_request_body, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True)

    def test_direct_request_query_detection(self):
        """Direct request.query access sets only the query flag."""
        analysis = analyze_handler(handler_reads_request_query, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True)

    def test_direct_request_cookies_detection(self):
        """Direct request.cookies access sets only the cookies flag."""
        analysis = analyze_handler(handler_reads_request_cookies, request_param_names={"request"})
        self.assert_request_flags(analysis, cookies=True)

    def test_request_meta_marks_headers_and_query(self):
        """request.META implies both header and query-string access."""
        analysis = analyze_handler(handler_reads_request_meta, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_get_full_path_marks_query_only(self):
        """request.get_full_path() requires query-string access only."""
        analysis = analyze_handler(handler_reads_request_full_path, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True)

    def test_request_build_absolute_uri_marks_headers_and_query(self):
        """request.build_absolute_uri() requires headers and query-string access."""
        analysis = analyze_handler(handler_reads_request_absolute_uri, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_get_known_keys_detection(self):
        """request.get(...) with known keys marks all referenced components."""
        analysis = analyze_handler(handler_reads_request_get_known_keys, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)

    def test_request_subscript_known_keys_detection(self):
        """request[...] with known keys marks all referenced components."""
        analysis = analyze_handler(handler_reads_request_subscript_known_keys, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)

    def test_nested_local_helper_detection(self):
        """Nested local helpers are still covered by the AST walk."""
        analysis = analyze_handler(handler_reads_request_in_local_helper, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_alias_name_detection(self):
        """Custom request parameter names are honored when passed to the analyzer."""
        analysis = analyze_handler(handler_reads_req_alias, request_param_names={"req"})
        self.assert_request_flags(analysis, headers=True, cookies=True)

    def test_unknown_request_keys_do_not_mark_components(self):
        """Unknown request.get/request[...] keys should not trigger component flags."""
        analysis = analyze_handler(handler_reads_request_unknown_keys, request_param_names={"request"})
        self.assert_request_flags(analysis)

    def test_a_request_handed_to_a_helper_needs_every_component(self):
        """The helper can read any part of the request, where this analysis does not look."""
        analysis = analyze_handler(handler_passes_request_to_external_helper, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)


class TestAnalysisFailure:
    """Tests for analysis failure handling."""

    def test_lambda_analysis_fails_gracefully(self):
        """Test that lambda functions fail gracefully."""
        handler = lambda: {"hello": "world"}  # noqa: E731

        analysis = analyze_handler(handler)

        # Lambdas may or may not be parseable depending on context
        # Either way, it shouldn't raise an exception
        assert isinstance(analysis, HandlerAnalysis)

    def test_source_that_does_not_parse_fails_the_analysis(self):
        analysis = analyze_handler(lambda_on_a_line_that_does_not_parse.args[0], {"request"})

        assert analysis.analysis_failed is True
        assert analysis.failure_reason.startswith("Syntax error parsing source: ")

    def test_builtin_function_fails_gracefully(self):
        """Test that built-in functions fail gracefully."""
        analysis = analyze_handler(len)

        assert analysis.analysis_failed is True
        assert analysis.failure_reason is not None
        assert "Could not get source" in analysis.failure_reason


class TestHandlerAnalysisProperties:
    """Tests for HandlerAnalysis computed properties."""

    def test_is_blocking_with_orm(self):
        """Test is_blocking returns True for any ORM usage."""
        analysis = HandlerAnalysis(uses_orm=True)
        assert analysis.is_blocking is True

    def test_is_blocking_with_blocking_io(self):
        """Test is_blocking returns True for blocking I/O."""
        analysis = HandlerAnalysis(has_blocking_io=True)
        assert analysis.is_blocking is True

    def test_is_blocking_with_neither(self):
        """Test is_blocking returns False when neither present."""
        analysis = HandlerAnalysis()
        assert analysis.is_blocking is False


# --- Support fixtures for dependency-tree analysis tests ---


def _dep_reads_query_via_request(request):
    return dict(request.query)


def _dep_reads_headers_via_request(request):
    return dict(request.headers)


def _dep_reads_cookies_via_request(request):
    return dict(request.cookies)


def _dep_reads_body_via_request(request):
    return request.body


def _dep_no_request_access(request):
    return {"hello": "world"}


class _CallableDepReadsQuery:
    def __call__(self, request):
        return dict(request.query)


class TestAnalyzeHandlerClassCallable:
    """analyze_handler must resolve class-callable instances to their __call__."""

    def test_class_callable_query_detection(self):
        instance = _CallableDepReadsQuery()
        analysis = analyze_handler(instance, request_param_names={"request"})
        assert analysis.analysis_failed is False
        assert analysis.request_needs_query is True


def _make_fake_field(*, source, dep_fn=None, name="dep"):
    """Build a minimal object with the attributes analyze_dependency_tree reads."""

    class FakeDependsMarker:
        def __init__(self, dependency):
            self.dependency = dependency

    class FakeField:
        pass

    f = FakeField()
    f.name = name
    f.source = source
    f.dependency = FakeDependsMarker(dep_fn) if dep_fn is not None else None
    return f


class TestAnalyzeDependencyTree:
    """analyze_dependency_tree walks Depends targets and aggregates needs."""

    def _compile_fn(self, fake_metas):
        """Return a compile_dep_fn that looks up pre-built metas by callable."""

        def compiler(fn):
            return fake_metas[fn]

        return compiler

    def test_no_dep_fields_produces_no_needs(self):
        meta = {"fields": []}
        needs = analyze_dependency_tree(meta, lambda _fn: {})
        assert needs == DependencyNeeds()

    def test_dep_reading_request_query_marks_query(self):
        dep_fn = _dep_reads_query_via_request
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_query is True
        assert needs.needs_headers is False
        assert needs.needs_cookies is False
        assert needs.needs_body is False

    def test_dep_reading_request_headers_marks_headers(self):
        dep_fn = _dep_reads_headers_via_request
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_headers is True
        assert needs.needs_query is False

    def test_dep_reading_request_cookies_marks_cookies(self):
        dep_fn = _dep_reads_cookies_via_request
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_cookies is True

    def test_dep_reading_request_body_marks_body(self):
        dep_fn = _dep_reads_body_via_request
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_body is True

    def test_dep_without_request_access_produces_no_needs(self):
        dep_fn = _dep_no_request_access
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs == DependencyNeeds()

    def test_typed_query_in_dep_is_picked_up_via_needs_query(self):
        """A dep with a typed Query() param already sets needs_query in its meta."""
        dep_fn = lambda page: {"page": page}  # noqa: E731
        dep_meta = {
            "mode": "mixed",
            "fields": [],
            "needs_query": True,
        }
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_query is True

    def test_compile_failure_marks_all_needs(self):
        """If the dep cannot be compiled, we must fall back to parsing everything."""
        dep_fn = _dep_reads_query_via_request
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        def failing_compiler(fn):
            raise TypeError("cannot introspect")

        needs = analyze_dependency_tree(handler_meta, failing_compiler)

        assert needs.needs_body is True
        assert needs.needs_query is True
        assert needs.needs_headers is True
        assert needs.needs_cookies is True

    def test_visited_set_prevents_infinite_recursion_on_self_reference(self):
        """A dep referenced twice is only analyzed once (by identity)."""
        dep_fn = _dep_reads_query_via_request
        dep_meta = {
            "mode": "request_only",
            "sig": inspect.signature(dep_fn),
            "fields": [],
        }
        handler_meta = {
            "fields": [
                _make_fake_field(source="dependency", dep_fn=dep_fn, name="a"),
                _make_fake_field(source="dependency", dep_fn=dep_fn, name="b"),
            ]
        }

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))
        assert needs.needs_query is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("use_handler", ["connection", "connections"])
def test_sync_handler_with_a_raw_cursor_runs_in_the_thread_pool(use_handler):
    """A raw cursor is blocking database work, as a manager call is."""
    api = BoltAPI()

    if use_handler == "connection":

        @api.get("/one")
        def one():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return {"value": cursor.fetchone()[0], "thread": threading.current_thread().name}
    else:

        @api.get("/one")
        def one():
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
                return {"value": cursor.fetchone()[0], "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/one")

    assert response.status_code == 200
    assert response.json()["value"] == 1
    # A sync handler that is not blocking work runs on the server thread, and
    # the cursor works there too. Only the thread tells the two apart.
    assert response.json()["thread"].startswith("bolt_default")


# A QuerySet that the handler does not get through ``.objects``.
active_users = User.objects.filter(is_active=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["generator", "set", "dict"])
def test_a_sync_handler_that_iterates_a_queryset_in_a_comprehension_runs_in_the_thread_pool(kind):
    """A list comprehension and a for loop over a QuerySet method count. Every comprehension must count."""
    api = BoltAPI()

    @api.get("/generator")
    def generator():
        return {"users": sum(1 for _user in active_users.all()), "thread": threading.current_thread().name}

    @api.get("/set")
    def set_comprehension():
        return {"users": len({user.pk for user in active_users.all()}), "thread": threading.current_thread().name}

    @api.get("/dict")
    def dict_comprehension():
        return {"users": len({user.pk: user for user in active_users.all()}), "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get(f"/{kind}")

    assert response.json()["users"] == 0
    assert response.json()["thread"].startswith("bolt_default")


TOTALS = {"a": 1, "b": 2}


def test_a_sync_handler_that_sums_dict_values_runs_on_the_server_thread():
    """A loop over dict.values() is not ORM work, so the handler does not need the thread pool."""
    api = BoltAPI()

    @api.get("/sum")
    def total():
        return {"total": sum(value for value in TOTALS.values()), "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/sum")

    assert response.json()["total"] == 3
    assert not response.json()["thread"].startswith("bolt_default")


EVERY_PART = ("body", "query", "headers", "cookies")


def flags(analysis):
    """The request parts that an analysis found, in a fixed order."""
    return tuple(part for part in EVERY_PART if getattr(analysis, f"request_needs_{part}"))


class TestOrmDetectionLimits:
    """A QuerySet method name counts only where it can be a database query."""

    def test_dict_methods_are_not_orm_work(self):
        def handler(data):
            total = len(data.get("items", []))
            first = data.copy().get("first")
            keys = [key.upper() for key, _value in data.items()]
            for _key, _value in data.items():
                total += 1
            return total, first, keys

        analysis = analyze_handler(handler)

        assert analysis.uses_orm is False
        assert analysis.orm_operations == set()
        assert analysis.is_blocking is False

    def test_a_chain_through_a_manager_counts_each_queryset_method(self):
        """select_for_update is not in the list, but the chain still reaches the manager."""

        def handler():
            return User.objects.filter(is_active=True).select_for_update().first()

        assert analyze_handler(handler).orm_operations == {"filter", "first"}

    def test_the_default_manager_is_a_manager(self):
        def handler():
            return User._default_manager.get(pk=1)

        analysis = analyze_handler(handler)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"get"}

    def test_async_model_instance_methods_are_orm_work(self):
        async def handler(user):
            await user.asave()

        analysis = analyze_handler(handler)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"asave"}

    def test_only_iteration_over_a_queryset_method_is_orm_work(self):
        def handler(users, items):
            for _item in items:
                pass
            return [item.pk for item in items], [user.pk for user in users.filter(is_active=True)]

        assert analyze_handler(handler).orm_operations == {"comprehension_filter"}

    def test_iteration_over_dict_values_is_not_orm_work(self):
        def handler(totals):
            for _value in totals.values():
                pass
            return [value * 2 for value in totals.values()], sum(value for value in totals.values())

        analysis = analyze_handler(handler)

        assert analysis.uses_orm is False
        assert analysis.orm_operations == set()

    def test_iteration_over_queryset_values_with_fields_is_orm_work(self):
        def handler(users):
            return sum(row["pk"] for row in users.values("pk"))

        assert analyze_handler(handler).orm_operations == {"comprehension_values"}

    @pytest.mark.parametrize("kind", ["for", "async for", "set", "dict", "generator"])
    def test_every_kind_of_iteration_over_a_queryset_is_orm_work(self, kind):
        async def for_loop(users):
            for user in users.filter(is_active=True):
                return user

        async def async_for_loop(users):
            async for user in users.filter(is_active=True):
                return user

        def set_comprehension(users):
            return {user.pk for user in users.filter(is_active=True)}

        def dict_comprehension(users):
            return {user.pk: user for user in users.filter(is_active=True)}

        def generator(users):
            return sum(user.pk for user in users.filter(is_active=True))

        handler, operation = {
            "for": (for_loop, "iterate_filter"),
            "async for": (async_for_loop, "iterate_filter"),
            "set": (set_comprehension, "comprehension_filter"),
            "dict": (dict_comprehension, "comprehension_filter"),
            "generator": (generator, "comprehension_filter"),
        }[kind]

        analysis = analyze_handler(handler)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {operation}

    @pytest.mark.parametrize("use_handler", ["connection", "connections"])
    def test_a_raw_cursor_is_orm_work(self, use_handler):
        def one():
            with connection.cursor() as cursor:
                return cursor.fetchone()

        def other():
            with connections["default"].cursor() as cursor:
                return cursor.fetchone()

        analysis = analyze_handler(one if use_handler == "connection" else other)

        assert analysis.uses_orm is True
        assert analysis.orm_operations == {"cursor"}

    def test_the_cursor_of_another_object_is_not_the_database(self):
        def handler(page, pages):
            return page.cursor, pages["next"].cursor

        assert analyze_handler(handler).uses_orm is False


class TestBlockingIoDetection:
    def test_builtin_blocking_calls_by_name(self):
        def handler(path):
            with open(path) as file:
                sleep(1)
                return file.read()

        analysis = analyze_handler(handler)

        assert analysis.has_blocking_io is True
        assert analysis.blocking_operations == {"open", "sleep"}

    def test_calls_that_do_no_io_are_not_blocking(self):
        """os.path and urllib.parse only compute. Neither is a blocking module."""

        def handler(self, make_client, name):
            return (
                os.path.join("a", name),
                urllib.parse.quote(name),
                self.client.get(name),
                make_client().get(name),
                len(name),
            )

        analysis = analyze_handler(handler)

        assert analysis.has_blocking_io is False
        assert analysis.blocking_operations == set()


class TestRequestPartReads:
    def test_raw_cookies_are_the_cookies_only(self):
        def handler(request):
            return request.raw_cookies

        assert flags(analyze_handler(handler, {"request"})) == ("cookies",)

    def test_request_attributes_that_hold_no_part_read_no_part(self):
        async def handler(request):
            user = await request.auser()
            return request.method, request.path, request.params, request.context, request.state, request.user, user

        assert flags(analyze_handler(handler, {"request"})) == ()

    def test_a_key_that_get_does_not_serve_reads_no_part(self):
        """request.get("META") gives the default. META is an attribute, not a key."""

        def handler(request):
            return request.get("META"), request.get("get_full_path"), request.get("raw_cookies")

        assert flags(analyze_handler(handler, {"request"})) == ()

    def test_a_get_with_a_default_reads_only_its_part(self):
        def handler(request):
            return request.get("headers", {})

        assert flags(analyze_handler(handler, {"request"})) == ("headers",)

    @pytest.mark.parametrize("access", ["get", "subscript"])
    def test_a_key_known_only_at_run_time_can_name_any_part(self, access):
        def by_get(request, key):
            return request.get(key)

        def by_subscript(request, key):
            return request[key]

        handler = by_get if access == "get" else by_subscript

        assert flags(analyze_handler(handler, {"request"})) == EVERY_PART

    def test_the_arguments_of_a_request_method_are_not_keys(self):
        def handler(request, location):
            return request.build_absolute_uri(location)

        assert flags(analyze_handler(handler, {"request"})) == ("query", "headers")

    def test_other_names_do_not_read_the_request(self):
        def handler(request, page, other):
            return page, other.headers, other.request.query, request.headers

        assert flags(analyze_handler(handler, {"request"})) == ("headers",)

    def test_a_handler_and_its_nested_function_both_count(self):
        def handler(request):
            def inner():
                return request.query

            return request.headers, inner()

        assert flags(analyze_handler(handler, {"request"})) == ("query", "headers")


class _Callable:
    def __call__(self, request):
        return request


class TestResolveIntrospectionTarget:
    def test_functions_methods_classes_and_modules_are_their_own_target(self):
        bound = _Callable().__call__

        assert resolve_introspection_target(sync_handler_no_orm) is sync_handler_no_orm
        assert resolve_introspection_target(bound) is bound
        assert resolve_introspection_target(_Callable) is _Callable
        assert resolve_introspection_target(json) is json

    def test_a_callable_instance_resolves_to_the_call_of_its_class(self):
        assert resolve_introspection_target(_Callable()) is _Callable.__call__

    def test_a_callable_in_c_resolves_to_the_slot_of_its_type(self):
        partial = functools.partial(sync_handler_no_orm)

        assert resolve_introspection_target(partial) is inspect.getattr_static(functools.partial, "__call__")


def _dependency(dep_fn, name="dep"):
    return _make_fake_field(source="dependency", dep_fn=dep_fn, name=name)


def _request_only(dep_fn):
    return {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}


def _mixed(*fields, **needs):
    return {"mode": "mixed", "fields": list(fields), **needs}


def _needs(needs):
    return tuple(part for part in EVERY_PART if getattr(needs, f"needs_{part}"))


def _typed_dep(value):
    return value


def _inner_dep(request):
    return request.cookies


def _outer_dep(inner):
    return inner


def _dep_with_request_and_typed_param(req, page):
    return req.headers, page


class TestDependencyTreeWalk:
    """analyze_dependency_tree adds the parts of every dependency, once each."""

    @pytest.mark.parametrize("part", EVERY_PART)
    def test_a_typed_parameter_of_a_dependency_needs_its_part(self, part):
        metas = {_typed_dep: _mixed(**{f"needs_{part}": True})}

        needs = analyze_dependency_tree({"fields": [_dependency(_typed_dep)]}, metas.__getitem__)

        assert _needs(needs) == (part,)

    @pytest.mark.parametrize("part", EVERY_PART)
    def test_a_later_dependency_does_not_clear_what_an_earlier_one_needs(self, part):
        reads_part = globals()[f"_dep_reads_{part}_via_request"]
        quiet = _dep_no_request_access
        typed_metas = {_typed_dep: _mixed(**{f"needs_{part}": True}), quiet: _request_only(quiet)}
        read_metas = {reads_part: _request_only(reads_part), quiet: _request_only(quiet)}

        typed = analyze_dependency_tree(
            {"fields": [_dependency(_typed_dep, name="typed"), _dependency(quiet, name="quiet")]},
            typed_metas.__getitem__,
        )
        read = analyze_dependency_tree(
            {"fields": [_dependency(reads_part, name="reads"), _dependency(quiet, name="quiet")]},
            read_metas.__getitem__,
        )

        assert _needs(typed) == (part,)
        assert _needs(read) == (part,)

    def test_fields_that_are_not_dependencies_are_skipped(self):
        fields = [_make_fake_field(source="query", name="page"), _dependency(_dep_reads_query_via_request)]
        metas = {_dep_reads_query_via_request: _request_only(_dep_reads_query_via_request)}

        needs = analyze_dependency_tree({"fields": fields}, metas.__getitem__)

        assert _needs(needs) == ("query",)

    def test_a_repeated_dependency_is_compiled_once_and_the_next_one_still_counts(self):
        metas = {
            _dep_reads_query_via_request: _request_only(_dep_reads_query_via_request),
            _dep_reads_cookies_via_request: _request_only(_dep_reads_cookies_via_request),
        }
        compiled = []

        def compile_dep(fn):
            compiled.append(fn)
            return metas[fn]

        fields = [
            _dependency(_dep_reads_query_via_request, name="a"),
            _dependency(_dep_reads_query_via_request, name="b"),
            _dependency(_dep_reads_cookies_via_request, name="c"),
        ]

        needs = analyze_dependency_tree({"fields": fields}, compile_dep)

        assert _needs(needs) == ("query", "cookies")
        assert compiled == [_dep_reads_query_via_request, _dep_reads_cookies_via_request]

    def test_a_dependency_that_depends_on_itself_is_walked_once(self):
        metas = {_typed_dep: _mixed(_dependency(_typed_dep), needs_body=True)}

        needs = analyze_dependency_tree({"fields": [_dependency(_typed_dep)]}, metas.__getitem__)

        assert _needs(needs) == ("body",)

    def test_the_dependencies_of_a_dependency_count(self):
        metas = {
            _outer_dep: _mixed(_dependency(_inner_dep, name="inner")),
            _inner_dep: _request_only(_inner_dep),
        }

        needs = analyze_dependency_tree({"fields": [_dependency(_outer_dep)]}, metas.__getitem__)

        assert _needs(needs) == ("cookies",)

    def test_the_request_parameter_of_a_dependency_with_typed_parameters(self):
        fields = (_make_fake_field(source="request", name="req"), _make_fake_field(source="query", name="page"))
        metas = {_dep_with_request_and_typed_param: _mixed(*fields, needs_query=True)}

        needs = analyze_dependency_tree({"fields": [_dependency(_dep_with_request_and_typed_param)]}, metas.__getitem__)

        assert _needs(needs) == ("query", "headers")

    def test_a_dependency_that_the_analysis_cannot_read_needs_every_part(self):
        """inspect.getsource raises for a functools.partial."""
        dep_fn = functools.partial(_dep_reads_query_via_request)
        metas = {dep_fn: _request_only(dep_fn)}

        needs = analyze_dependency_tree({"fields": [_dependency(dep_fn)]}, metas.__getitem__)

        assert _needs(needs) == EVERY_PART
