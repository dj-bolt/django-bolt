"""App under test for a route behind middleware whose handler does not read the body.

The middleware reads the body, so the route must get it:

- ``CsrfViewMiddleware`` reads the form token from ``request.POST``. The
  routes at the root are behind ``django_middleware=``.
- A Python middleware under ``/python`` sends the body back in a header.
"""

from __future__ import annotations

from django.middleware.csrf import get_token

from django_bolt import BoltAPI
from django_bolt.middleware import middleware

api = BoltAPI(django_middleware=["django.middleware.csrf.CsrfViewMiddleware"])
python_api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/token")
async def token(request):
    return {"token": get_token(request)}


@api.post("/submit")
async def submit():
    return {"status": "submitted"}


@api.post("/submit-sync")
def submit_sync():
    return {"status": "submitted"}


@middleware
async def body_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Body"] = request.body.decode()
    return response


@python_api.post("/echo")
@body_header
async def echo():
    return {"status": "ok"}


api.mount("/python", python_api)
