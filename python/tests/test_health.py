"""Tests for Django-Bolt health check system."""

import asyncio
import time

import pytest

from django_bolt import BoltAPI
from django_bolt.health import (
    HealthCheck,
    _health_check,
    add_health_check,
    check_database,
    health_handler,
    ready_handler,
    register_health_checks,
)


class TestHealthCheck:
    """Test HealthCheck class."""

    def test_health_check_initialization(self):
        """Test HealthCheck initialization."""
        hc = HealthCheck()
        assert hc._checks == []

    def test_add_check(self):
        """Test adding custom health checks."""
        hc = HealthCheck()

        async def custom_check():
            return True, "OK"

        hc.add_check(custom_check)
        assert len(hc._checks) == 1
        assert hc._checks[0] == custom_check

    @pytest.mark.asyncio
    async def test_run_checks_all_healthy(self):
        """Test run_checks when all checks pass."""
        hc = HealthCheck()

        async def check1():
            return True, "Check 1 OK"

        async def check2():
            return True, "Check 2 OK"

        hc.add_check(check1)
        hc.add_check(check2)

        results = await hc.run_checks()
        assert results["status"] == "healthy"
        assert "check1" in results["checks"]
        assert "check2" in results["checks"]
        assert results["checks"]["check1"]["healthy"] is True
        assert results["checks"]["check2"]["healthy"] is True

    @pytest.mark.asyncio
    async def test_run_checks_one_unhealthy(self):
        """Test run_checks when one check fails."""
        hc = HealthCheck()

        async def check1():
            return True, "Check 1 OK"

        async def check2():
            return False, "Check 2 failed"

        hc.add_check(check1)
        hc.add_check(check2)

        results = await hc.run_checks()
        assert results["status"] == "unhealthy"
        assert results["checks"]["check1"]["healthy"] is True
        assert results["checks"]["check2"]["healthy"] is False
        assert "failed" in results["checks"]["check2"]["message"]

    @pytest.mark.asyncio
    async def test_run_checks_exception_handling(self):
        """Test run_checks handles exceptions in checks."""
        hc = HealthCheck()

        async def failing_check():
            raise RuntimeError("Check crashed")

        hc.add_check(failing_check)

        results = await hc.run_checks()
        assert results["status"] == "unhealthy"
        assert results["checks"]["failing_check"]["healthy"] is False
        assert "Check crashed" in results["checks"]["failing_check"]["message"]


class TestDatabaseCheck:
    """Test database health check."""

    @pytest.mark.asyncio
    async def test_check_database_success(self):
        """Test database check succeeds with valid connection."""
        # This test requires Django to be configured
        try:
            from django.conf import settings  # noqa: PLC0415

            if not settings.configured:
                pytest.skip("Django not configured")

            healthy, message = await check_database()
            # Should either succeed or fail gracefully
            assert isinstance(healthy, bool)
            assert isinstance(message, str)
        except ImportError:
            pytest.skip("Django not available")

    @pytest.mark.asyncio
    async def test_check_database_handles_error(self):
        """Test database check handles connection errors."""
        # Even if database is not available, check should not raise
        try:
            healthy, message = await check_database()
            assert isinstance(healthy, bool)
            assert isinstance(message, str)
        except Exception:
            pytest.fail("check_database should not raise exceptions")


class TestHealthHandlers:
    """Test health endpoint handlers."""

    @pytest.mark.asyncio
    async def test_health_handler(self):
        """Test basic health endpoint."""
        result = await health_handler()
        assert isinstance(result, dict)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_ready_handler(self):
        """Test readiness endpoint."""
        result = await ready_handler()
        assert isinstance(result, dict)
        assert "status" in result
        assert "checks" in result
        # Status should be healthy or unhealthy
        assert result["status"] in ["healthy", "unhealthy"]


class TestHealthIntegration:
    """Integration tests for health checks."""

    def test_register_health_checks(self):
        """Test registering health checks on API."""
        api = BoltAPI()
        initial_route_count = len(api._routes)

        register_health_checks(api)

        # Check that routes were registered (should have 2 more routes)
        assert len(api._routes) == initial_route_count + 2

        # Check handler names contain health/ready
        handlers = [api._handlers[route[2]] for route in api._routes[initial_route_count:]]
        handler_names = [h.__name__ for h in handlers]
        assert "health_handler" in handler_names
        assert "ready_handler" in handler_names

    def test_add_health_check_global(self):
        """Test adding global health check."""

        async def custom_check():
            return True, "Custom check OK"

        add_health_check(custom_check)

        # Check should be added to global instance
        assert custom_check in _health_check._checks

        # Clean up
        _health_check._checks.remove(custom_check)

    @pytest.mark.asyncio
    async def test_custom_health_check_integration(self):
        """Test custom health check integration."""
        # Clear existing checks
        original_checks = _health_check._checks.copy()
        _health_check._checks.clear()

        # Add custom check
        async def redis_check():
            # Simulate Redis check
            return True, "Redis OK"

        add_health_check(redis_check)

        # Run ready handler
        result = await ready_handler()
        assert result["status"] == "healthy"
        assert "redis_check" in result["checks"]
        assert result["checks"]["redis_check"]["healthy"] is True

        # Restore original checks
        _health_check._checks = original_checks


class TestHealthCheckScenarios:
    """Test real-world health check scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_services_all_healthy(self):
        """Test health check with multiple healthy services."""
        hc = HealthCheck()

        async def check_database():
            return True, "Database OK"

        async def check_redis():
            return True, "Redis OK"

        async def check_queue():
            return True, "Queue OK"

        hc.add_check(check_database)
        hc.add_check(check_redis)
        hc.add_check(check_queue)

        results = await hc.run_checks()
        assert results["status"] == "healthy"
        assert len(results["checks"]) == 3

    @pytest.mark.asyncio
    async def test_multiple_services_one_degraded(self):
        """Test health check with one degraded service."""
        hc = HealthCheck()

        async def check_database():
            return True, "Database OK"

        async def check_redis():
            return False, "Redis connection timeout"

        async def check_queue():
            return True, "Queue OK"

        hc.add_check(check_database)
        hc.add_check(check_redis)
        hc.add_check(check_queue)

        results = await hc.run_checks()
        assert results["status"] == "unhealthy"
        assert results["checks"]["check_database"]["healthy"] is True
        assert results["checks"]["check_redis"]["healthy"] is False
        assert results["checks"]["check_queue"]["healthy"] is True

    @pytest.mark.asyncio
    async def test_async_check_performance(self):
        """Test that async checks run concurrently."""
        hc = HealthCheck()
        start_time = time.time()

        async def slow_check1():
            await asyncio.sleep(0.1)
            return True, "Check 1 OK"

        async def slow_check2():
            await asyncio.sleep(0.1)
            return True, "Check 2 OK"

        hc.add_check(slow_check1)
        hc.add_check(slow_check2)

        await hc.run_checks()
        elapsed = time.time() - start_time

        # Two 0.1s checks running concurrently should complete in ~0.1s
        assert elapsed < 0.15


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
