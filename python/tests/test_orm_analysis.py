"""
Tests for static analysis module.

Tests the AST-based analysis of handler functions for:
- Django ORM usage detection
- Blocking I/O detection
- Warning generation for sync handlers
"""

from __future__ import annotations

import warnings

from django_bolt.analysis import (
    HandlerAnalysis,
    analyze_handler,
    warn_blocking_handler,
)


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


class TestHandlerAnalysis:
    """Tests for analyze_handler function."""

    def test_sync_handler_with_orm_detection(self):
        """Test that ORM usage is correctly detected."""
        analysis = analyze_handler(sync_handler_with_orm)

        assert analysis.uses_orm is True
        assert "filter" in analysis.orm_operations
        assert analysis.is_blocking is True
        assert analysis.analysis_failed is False

    def test_sync_handler_with_multiple_orm_calls(self):
        """Test detection of multiple ORM operations."""
        analysis = analyze_handler(sync_handler_with_multiple_orm_calls)

        assert analysis.uses_orm is True
        assert "create" in analysis.orm_operations
        assert "all" in analysis.orm_operations
        assert "count" in analysis.orm_operations
        assert "first" in analysis.orm_operations

    def test_async_handler_with_sync_orm(self):
        """Test that ORM in async handler is detected."""
        analysis = analyze_handler(async_handler_with_sync_orm)

        assert analysis.uses_orm is True
        assert "filter" in analysis.orm_operations

    def test_async_handler_with_async_orm(self):
        """Test that async ORM methods are correctly identified."""
        analysis = analyze_handler(async_handler_with_async_orm)

        assert analysis.uses_orm is True
        assert "aget" in analysis.orm_operations
        assert "aiterator" in analysis.orm_operations

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
        # Should detect 'all' and the iteration
        assert "all" in analysis.orm_operations or "iterate_all" in analysis.orm_operations

    def test_list_comprehension_over_queryset(self):
        """Test detection of list comprehension over QuerySet."""
        analysis = analyze_handler(sync_handler_with_list_comprehension)

        assert analysis.uses_orm is True

    def test_model_save_detection(self):
        """Test detection of model.save() calls."""
        analysis = analyze_handler(sync_handler_with_save)

        assert analysis.uses_orm is True
        assert "save" in analysis.orm_operations

    def test_blocking_io_detection(self):
        """Test detection of blocking I/O operations."""
        analysis = analyze_handler(sync_handler_with_blocking_io)

        assert analysis.has_blocking_io is True
        assert "time.sleep" in analysis.blocking_operations
        assert analysis.is_blocking is True

    def test_requests_library_detection(self):
        """Test detection of requests library usage."""
        analysis = analyze_handler(sync_handler_with_requests)

        assert analysis.has_blocking_io is True
        assert "requests.get" in analysis.blocking_operations


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

    def test_external_helper_usage_is_not_currently_inferred(self):
        """Document current limitation: external helper calls are not analyzed transitively."""
        analysis = analyze_handler(handler_passes_request_to_external_helper, request_param_names={"request"})
        self.assert_request_flags(analysis)


class TestWarningGeneration:
    """Tests for warning message generation."""

    def test_sync_handler_orm_warning(self):
        """Test warning is generated for sync handler with ORM."""
        analysis = analyze_handler(sync_handler_with_orm)

        warning_msg = analysis.get_warning_message(handler_name="sync_handler_with_orm", path="/users", is_async=False)

        assert warning_msg is not None
        assert "sync_handler_with_orm" in warning_msg
        assert "/users" in warning_msg
        assert "Running in thread pool" in warning_msg

    def test_async_handler_no_warning(self):
        """Test no warning for async handler - Django handles sync ORM automatically."""
        analysis = analyze_handler(async_handler_with_sync_orm)

        warning_msg = analysis.get_warning_message(
            handler_name="async_handler_with_sync_orm", path="/async-users", is_async=True
        )

        # Async handlers don't need warnings - Django handles sync-to-async
        assert warning_msg is None

    def test_no_warning_for_clean_handler(self):
        """Test no warning for handlers without issues."""
        analysis = analyze_handler(sync_handler_no_orm)

        warning_msg = analysis.get_warning_message(handler_name="sync_handler_no_orm", path="/hello", is_async=False)

        assert warning_msg is None

    def test_no_warning_for_async_with_async_orm(self):
        """Test no warning for proper async ORM usage."""
        analysis = analyze_handler(async_handler_with_async_orm)

        warning_msg = analysis.get_warning_message(
            handler_name="async_handler_with_async_orm", path="/async-users", is_async=True
        )

        # Should not warn if using async ORM (even if also detected sync patterns)
        # Actually our current logic will still warn - let's check
        # The warning should be None if uses_async_orm is True
        # Looking at the code, it warns if sync ORM detected without async ORM
        # Since this handler uses async ORM, it should not warn
        assert warning_msg is None

    def test_warn_blocking_handler_emits_warning(self):
        """Test that warn_blocking_handler actually emits warnings for sync handlers."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            warn_blocking_handler(fn=sync_handler_with_orm, path="/users", is_async=False)

            # Check that a warning was emitted
            assert len(w) == 1
            assert "Running in thread pool" in str(w[0].message)


class TestAnalysisFailure:
    """Tests for analysis failure handling."""

    def test_lambda_analysis_fails_gracefully(self):
        """Test that lambda functions fail gracefully."""
        handler = lambda: {"hello": "world"}  # noqa: E731

        analysis = analyze_handler(handler)

        # Lambdas may or may not be parseable depending on context
        # Either way, it shouldn't raise an exception
        assert isinstance(analysis, HandlerAnalysis)

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
