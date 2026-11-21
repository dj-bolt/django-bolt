"""App whose API has a Django middleware: it gets the requests that Rust answers.

``/sync`` has a sync handler, so a request runs on a request lane.
"""

from __future__ import annotations

import threading

from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.logging import LoggingConfig
from django_bolt.middleware import DjangoMiddleware, rate_limit

_state = threading.local()


class TenantMiddleware(MiddlewareMixin):
    """Keep a value in a thread-local, as django-tenants does, and send it back."""

    def process_request(self, request):
        _state.tenant = "acme"

    def process_response(self, request, response):
        response["X-Tenant"] = getattr(_state, "tenant", None) or "lost"
        _state.tenant = None
        return response


api = BoltAPI(
    middleware=[DjangoMiddleware(TenantMiddleware)],
    logging_config=LoggingConfig(skip_paths={"/health", "/skipped"}),
)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/open")
async def open_route():
    return {"ok": True}


@api.get("/sync")
def sync_route():
    return {"ok": True}


@api.get("/private", auth=[APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")], guards=[IsAuthenticated()])
def private():
    return {"ok": True}


@api.get("/limited")
@rate_limit(rps=1, burst=1, key="ip")
async def limited():
    return {"ok": True}
