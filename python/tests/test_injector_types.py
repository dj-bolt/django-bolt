"""Tests for injector return type contracts.

The Rust WebSocket dispatcher iterates injector args via try_iter() which
accepts any iterable (tuple or list). These tests verify the contract that
args are always iterable sequences.

Regression test for #172.
"""

from __future__ import annotations

from typing import Any

from django_bolt._kwargs.model import (
    _NO_PARAMS,
    HandlerPattern,
    _injector_no_params,
    compile_argument_injector,
    compile_binder,
)


class TestNoParamsInjector:
    """Verify _NO_PARAMS and _injector_no_params return valid arg types."""

    def test_no_params_args_is_tuple(self):
        args, kwargs = _NO_PARAMS
        assert isinstance(args, tuple), (
            f"_NO_PARAMS args should be a tuple for immutability, got {type(args).__name__}"
        )

    def test_no_params_args_is_empty(self):
        args, _ = _NO_PARAMS
        assert len(args) == 0

    def test_no_params_kwargs_is_dict(self):
        _, kwargs = _NO_PARAMS
        assert isinstance(kwargs, dict)

    def test_injector_no_params_returns_tuple_args(self):
        args, kwargs = _injector_no_params(None)
        assert isinstance(args, tuple), (
            f"_injector_no_params should return tuple args, got {type(args).__name__}"
        )


class TestCompiledInjectorTypes:
    """Verify compiled injectors return iterable args (list or tuple)."""

    def _make_meta(self, pattern: HandlerPattern) -> dict[str, Any]:
        """Create minimal handler metadata for injector compilation."""
        return {
            "fields": [],
            "path_params": set(),
            "http_method": "WEBSOCKET",
            "handler_pattern": pattern,
            "needs_body": False,
            "needs_query": False,
            "needs_headers": False,
            "needs_cookies": False,
            "needs_path_params": False,
            "needs_form_parsing": False,
            "mode": "websocket_only",
        }

    def test_no_params_pattern_returns_iterable(self):
        meta = self._make_meta(HandlerPattern.NO_PARAMS)
        injector = compile_argument_injector(meta, {}, compile_binder)
        args, kwargs = injector({"path_params": {}, "query_params": {}})
        assert isinstance(args, (list, tuple)), (
            f"NO_PARAMS injector must return list or tuple args, got {type(args).__name__}"
        )
