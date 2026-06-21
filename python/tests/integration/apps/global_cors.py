"""App under test for global CORS applied from Django settings at startup.

The allowed origin is configured via ``CORS_ALLOWED_ORIGINS`` in the test's
``settings_extra``; this route just confirms the header is applied.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/global-cors")
async def global_cors():
    return {"ok": True}
