"""App under test for ``--dev`` ignoring non-Python/non-HTML file changes.

Reports the worker PID and the dev reload counter so the test can assert that
touching a ``.log`` file does NOT trigger a reload (PID and count unchanged).
"""

from __future__ import annotations

import os

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/reload-state")
async def reload_state():
    return {
        "pid": os.getpid(),
        "reload_count": int(os.environ.get("DJANGO_BOLT_DEV_RELOAD_COUNT", "0")),
    }
