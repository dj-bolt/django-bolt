"""Router has no router-level dependencies."""

from __future__ import annotations

import pytest

from django_bolt import Depends, Router


def test_router_refuses_dependencies_that_it_cannot_run():
    """Bolt has no router-level dependencies. An argument that Bolt would ignore must not pass."""

    async def require_user():
        raise AssertionError("not reached")

    with pytest.raises(TypeError, match="dependencies"):
        Router(prefix="/admin", dependencies=[Depends(require_user)])
