"""Health check endpoints for Django-Bolt.

Provides standard health check endpoints for monitoring and load balancers.
"""

import asyncio
from collections.abc import Callable
from typing import Any

try:
    from asgiref.sync import sync_to_async
    from django.db import connection
except ImportError:
    sync_to_async = None
    connection = None


class HealthCheck:
    """Health check configuration and helpers."""

    def __init__(self):
        self._checks: list[Callable] = []

    def add_check(self, check_func: Callable) -> None:
        """Add a custom health check.

        Args:
            check_func: Async function that returns (bool, str) - (is_healthy, message)
        """
        self._checks.append(check_func)

    async def run_checks(self) -> dict[str, Any]:
        """Run all configured health checks.

        Returns:
            Dictionary with health check results
        """
        results_list = await asyncio.gather(
            *(check() for check in self._checks),
            return_exceptions=True,
        )

        results = {}
        all_healthy = True

        for check, result in zip(self._checks, results_list):
            if isinstance(result, Exception):
                results[check.__name__] = {
                    "healthy": False,
                    "message": f"Check failed: {result!s}",
                }
                all_healthy = False
            else:
                is_healthy, message = result
                results[check.__name__] = {
                    "healthy": is_healthy,
                    "message": message,
                }
                if not is_healthy:
                    all_healthy = False

        return {
            "status": "healthy" if all_healthy else "unhealthy",
            "checks": results,
        }


# Global health check instance
_health_check = HealthCheck()


async def check_database() -> tuple[bool, str]:
    """Check database connectivity.

    Returns:
        Tuple of (is_healthy, message)
    """
    try:
        if sync_to_async is None or connection is None:
            return False, "Django not available"

        await sync_to_async(connection.ensure_connection)()

        return True, "Database connection OK"
    except Exception as e:
        return False, f"Database error: {e!s}"


# Health endpoint handlers


async def health_handler() -> dict[str, str]:
    """Simple liveness check.

    Returns:
        Dictionary with status
    """
    return {"status": "ok"}


async def ready_handler() -> dict[str, Any]:
    """Readiness check with dependency checks.

    Returns:
        Dictionary with readiness status and checks
    """
    # Add database check by default
    if not _health_check._checks:
        _health_check.add_check(check_database)

    results = await _health_check.run_checks()
    return results


def register_health_checks(api) -> None:
    """Register health check endpoints on a BoltAPI instance.

    Args:
        api: BoltAPI instance
    """
    api.get("/health")(health_handler)
    api.get("/ready")(ready_handler)


def add_health_check(check_func: Callable) -> None:
    """Add a custom health check.

    Args:
        check_func: Async function that returns (bool, str)

    Example:
        ```python
        from django_bolt.health import add_health_check

        async def check_redis():
            # Check redis connection
            return True, "Redis OK"

        add_health_check(check_redis)
        ```
    """
    _health_check.add_check(check_func)
