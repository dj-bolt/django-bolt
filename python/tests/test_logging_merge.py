"""Integration tests for logging with merged APIs.

Tests that per-API logging configurations are preserved when multiple
BoltAPI instances are merged (like during autodiscovery in runbolt).
"""

import logging

import pytest

from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig


class TestMergedAPILoggingPreservation:
    """Test that merged APIs preserve per-API logging configs."""

    def test_handler_maps_to_original_api_after_merge(self):
        """Each handler should map back to its original API instance after merge."""
        # Create API 1 with custom logging
        logging_config_1 = LoggingConfig(
            logger_name="api1_logger",
            skip_status_codes={204},
        )
        api1 = BoltAPI(logging_config=logging_config_1)

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        # Create API 2 with different logging
        logging_config_2 = LoggingConfig(
            logger_name="api2_logger",
            skip_status_codes={304},
        )
        api2 = BoltAPI(logging_config=logging_config_2)

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Simulate merge (like runbolt does with handler_id renumbering)
        merged = BoltAPI(enable_logging=False)
        merged._handler_api_map = {}

        next_handler_id = 0

        # Merge routes from api1 (renumber handler_ids to avoid collisions)
        for method, path, old_handler_id, handler in api1._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api1
            # Copy handler metadata using the old handler_id from api1
            if old_handler_id in api1._handler_meta:
                merged._handler_meta[new_handler_id] = api1._handler_meta[old_handler_id]

        # Merge routes from api2 (renumber handler_ids to avoid collisions)
        for method, path, old_handler_id, handler in api2._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api2
            # Copy handler metadata using the old handler_id from api2
            if old_handler_id in api2._handler_meta:
                merged._handler_meta[new_handler_id] = api2._handler_meta[old_handler_id]

        merged._next_handler_id = next_handler_id

        # Verify handler 0 (from api1) maps to api1
        handler_id_api1 = 0
        assert handler_id_api1 in merged._handler_api_map, f"handler_id {handler_id_api1} must exist in map"

        mapped_api1 = merged._handler_api_map[handler_id_api1]
        assert id(mapped_api1) == id(api1), (
            f"Handler 0 must map to api1 (expected id={id(api1)}, got id={id(mapped_api1)})"
        )

        # Verify api1's logging config is preserved
        assert mapped_api1._logging_middleware is not None, "api1 must have logging middleware"
        assert mapped_api1._logging_middleware.config.logger_name == "api1_logger", (
            "api1's logger name must be preserved"
        )
        assert mapped_api1._logging_middleware.config.skip_status_codes == {204}, (
            "api1's skip_status_codes must be preserved"
        )

        # Verify handler 1 (from api2) maps to api2
        handler_id_api2 = 1
        assert handler_id_api2 in merged._handler_api_map, f"handler_id {handler_id_api2} must exist in map"

        mapped_api2 = merged._handler_api_map[handler_id_api2]
        assert id(mapped_api2) == id(api2), (
            f"Handler 1 must map to api2 (expected id={id(api2)}, got id={id(mapped_api2)})"
        )

        # Verify api2's logging config is preserved
        assert mapped_api2._logging_middleware is not None, "api2 must have logging middleware"
        assert mapped_api2._logging_middleware.config.logger_name == "api2_logger", (
            "api2's logger name must be preserved"
        )
        assert mapped_api2._logging_middleware.config.skip_status_codes == {304}, (
            "api2's skip_status_codes must be preserved"
        )

    def test_merged_api_with_different_skip_paths(self):
        """Each API's skip_paths should be preserved independently."""
        # API 1 skips /health
        config1 = LoggingConfig(skip_paths={"/health"})
        api1 = BoltAPI(logging_config=config1)

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        # API 2 skips /metrics
        config2 = LoggingConfig(skip_paths={"/metrics"})
        api2 = BoltAPI(logging_config=config2)

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Merge
        merged = BoltAPI(enable_logging=False)
        merged._handler_api_map = {}
        next_handler_id = 0

        for method, path, old_handler_id, handler in api1._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api1
            # Copy handler metadata
            if old_handler_id in api1._handler_meta:
                merged._handler_meta[new_handler_id] = api1._handler_meta[old_handler_id]

        for method, path, old_handler_id, handler in api2._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api2
            # Copy handler metadata
            if old_handler_id in api2._handler_meta:
                merged._handler_meta[new_handler_id] = api2._handler_meta[old_handler_id]

        # Verify each API retains its own skip_paths
        api1_from_map = merged._handler_api_map[0]
        api2_from_map = merged._handler_api_map[1]

        assert "/health" in api1_from_map._logging_middleware.config.skip_paths
        assert "/health" not in api2_from_map._logging_middleware.config.skip_paths
        assert "/metrics" not in api1_from_map._logging_middleware.config.skip_paths
        assert "/metrics" in api2_from_map._logging_middleware.config.skip_paths

    def test_merged_api_with_different_sample_rates(self):
        """Each API's sample_rate should be preserved independently."""
        # API 1 samples at 0.05
        config1 = LoggingConfig(sample_rate=0.05)
        api1 = BoltAPI(logging_config=config1)

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        # API 2 has no sampling
        config2 = LoggingConfig(sample_rate=None)
        api2 = BoltAPI(logging_config=config2)

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Merge
        merged = BoltAPI(enable_logging=False)
        merged._handler_api_map = {}
        next_handler_id = 0

        for method, path, old_handler_id, handler in api1._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api1
            # Copy handler metadata
            if old_handler_id in api1._handler_meta:
                merged._handler_meta[new_handler_id] = api1._handler_meta[old_handler_id]

        for method, path, old_handler_id, handler in api2._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api2
            # Copy handler metadata
            if old_handler_id in api2._handler_meta:
                merged._handler_meta[new_handler_id] = api2._handler_meta[old_handler_id]

        # Verify each API retains its own sample_rate
        api1_from_map = merged._handler_api_map[0]
        api2_from_map = merged._handler_api_map[1]

        assert api1_from_map._logging_middleware.config.sample_rate == 0.05
        assert api2_from_map._logging_middleware.config.sample_rate is None

    def test_merged_api_with_different_min_duration_thresholds(self):
        """Each API's min_duration_ms should be preserved independently."""
        # API 1 logs only slow requests (500ms+)
        config1 = LoggingConfig(min_duration_ms=500)
        api1 = BoltAPI(logging_config=config1)

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        # API 2 logs all requests (no threshold)
        config2 = LoggingConfig(min_duration_ms=None)
        api2 = BoltAPI(logging_config=config2)

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Merge
        merged = BoltAPI(enable_logging=False)
        merged._handler_api_map = {}
        next_handler_id = 0

        for method, path, old_handler_id, handler in api1._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api1
            # Copy handler metadata
            if old_handler_id in api1._handler_meta:
                merged._handler_meta[new_handler_id] = api1._handler_meta[old_handler_id]

        for method, path, old_handler_id, handler in api2._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api2
            # Copy handler metadata
            if old_handler_id in api2._handler_meta:
                merged._handler_meta[new_handler_id] = api2._handler_meta[old_handler_id]

        # Verify each API retains its own min_duration_ms
        api1_from_map = merged._handler_api_map[0]
        api2_from_map = merged._handler_api_map[1]

        assert api1_from_map._logging_middleware.config.min_duration_ms == 500
        assert api2_from_map._logging_middleware.config.min_duration_ms is None

    def test_merged_api_one_with_logging_one_without(self):
        """API with logging and API without logging should coexist."""
        # API 1 with logging
        config1 = LoggingConfig(logger_name="api1_logger")
        api1 = BoltAPI(logging_config=config1)

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        # API 2 without logging
        api2 = BoltAPI(enable_logging=False)

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Merge
        merged = BoltAPI(enable_logging=False)
        merged._handler_api_map = {}
        next_handler_id = 0

        for method, path, old_handler_id, handler in api1._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api1
            # Copy handler metadata
            if old_handler_id is not None and old_handler_id in api1._handler_meta:
                merged._handler_meta[new_handler_id] = api1._handler_meta[old_handler_id]
        for method, path, old_handler_id, handler in api2._routes:
            new_handler_id = next_handler_id
            next_handler_id += 1
            merged._routes.append((method, path, new_handler_id, handler))
            merged._handlers[new_handler_id] = handler
            merged._handler_api_map[new_handler_id] = api2
            # Copy handler metadata
            if old_handler_id is not None and old_handler_id in api2._handler_meta:
                merged._handler_meta[new_handler_id] = api2._handler_meta[old_handler_id]
        # Verify api1 has logging, api2 doesn't
        api1_from_map = merged._handler_api_map[0]
        api2_from_map = merged._handler_api_map[1]

        assert api1_from_map._logging_middleware is not None, "api1 must have logging middleware"
        assert api2_from_map._logging_middleware is None, "api2 must not have logging middleware"


class TestAPIDeduplication:
    """Test that duplicate API instances are properly deduplicated."""

    def test_deduplication_by_object_identity(self):
        """Duplicate API instances (same object) should be deduplicated by id()."""
        # Create one API
        api1 = BoltAPI()

        @api1.get("/test")
        async def handler():
            return {"test": True}

        # Simulate autodiscovery finding the SAME api object twice
        apis = [
            ("testproject.api:api", api1),
            ("testproject.api:api", api1),  # Same object reference
        ]

        # Deduplicate
        seen_ids = set()
        deduplicated = []
        for api_path, api in apis:
            api_id = id(api)
            if api_id not in seen_ids:
                seen_ids.add(api_id)
                deduplicated.append((api_path, api))

        # Should only have 1 entry
        assert len(deduplicated) == 1, "Duplicate API instances must be deduplicated"
        assert deduplicated[0] == ("testproject.api:api", api1)

    def test_different_instances_are_not_deduplicated(self):
        """Different API instances should NOT be deduplicated."""
        # Create two different APIs
        api1 = BoltAPI()

        @api1.get("/api1")
        async def handler1():
            return {"api": 1}

        api2 = BoltAPI()

        @api2.get("/api2")
        async def handler2():
            return {"api": 2}

        # Both should be kept (different objects)
        apis = [
            ("app1.api:api", api1),
            ("app2.api:api", api2),
        ]

        # Deduplicate
        seen_ids = set()
        deduplicated = []
        for api_path, api in apis:
            api_id = id(api)
            if api_id not in seen_ids:
                seen_ids.add(api_id)
                deduplicated.append((api_path, api))

        # Should have both entries (different objects)
        assert len(deduplicated) == 2, "Different API instances must not be deduplicated"
        assert id(deduplicated[0][1]) != id(deduplicated[1][1])


class TestLoggingWithHandlerCalls:
    """Test that logging actually works when handlers are called."""

    def test_logging_middleware_logs_on_handler_call(self, caplog):
        """Logging middleware should log when handler is invoked."""
        config = LoggingConfig(logger_name="test.api", min_duration_ms=None)
        api = BoltAPI(logging_config=config)

        @api.get("/test")
        async def test_handler():
            return {"result": "success"}

        # Simulate calling the handler (simplified)
        request = {
            "method": "GET",
            "path": "/test",
            "query_params": {},
            "headers": {},
        }

        # log_response() is deprecated. The test covers it until a release removes it.
        with (
            caplog.at_level(logging.INFO, logger="test.api"),
            pytest.warns(DeprecationWarning, match=r"log_response\(\) is deprecated"),
        ):
            api._logging_middleware.log_response(request, 200, 0.1)

        # Should have logged
        assert len(caplog.records) > 0, "Handler response should be logged"
        assert caplog.records[0].status_code == 200

    def test_logging_middleware_logs_exceptions(self, caplog):
        """Logging middleware should log exceptions."""
        config = LoggingConfig(logger_name="test.api")
        api = BoltAPI(logging_config=config)

        @api.get("/error")
        async def error_handler():
            raise ValueError("Test error")

        # Simulate exception logging
        request = {
            "method": "GET",
            "path": "/error",
        }

        exc = ValueError("Test error")

        with caplog.at_level(logging.ERROR, logger="test.api"):
            api._logging_middleware.log_exception(request, exc, exc_info=False)

        # Should have logged exception
        assert len(caplog.records) > 0, "Exception should be logged"
        assert "ValueError" in caplog.records[0].message
        assert "Test error" in caplog.records[0].message

    def test_disabled_logging_does_not_log(self, caplog):
        """API with logging disabled should not log."""
        api = BoltAPI(enable_logging=False)

        @api.get("/test")
        async def test_handler():
            return {"result": "success"}

        # Verify no logging middleware
        assert api._logging_middleware is None, "API with enable_logging=False must not have logging middleware"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
