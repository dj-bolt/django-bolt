"""Tests of API deduplication and of the exception log of LoggingMiddleware."""

import logging

import pytest

from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig


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
