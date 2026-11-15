from __future__ import annotations
from django_bolt import BoltAPI, Request
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.testing import TestClient
from django_bolt.responses import JSON

class Spy:
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        request.spy = {"scheme": request.scheme, "secure": request.is_secure(), "host": request.get_host(), "uri": request.build_absolute_uri(), "ip": request.META.get("REMOTE_ADDR"), "wsgi": request.META.get("wsgi.url_scheme")}
        return self.get_response(request)

def test_p(settings):
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    settings.ALLOWED_HOSTS = ["*"]
    api = BoltAPI(middleware=[DjangoMiddlewareStack([Spy])])
    @api.get("/p")
    async def g(request: Request):
        return JSON(request.state.get("spy"))
    with TestClient(api) as c:
        print("S", c.get("/p", headers={"X-Forwarded-Proto": "https", "Host": "h.example"}).json())
        print("S", c.get("/p", headers={"X-Forwarded-For": "9.9.9.9"}).json())
        print("S", c.get("https://secure.example/p").json())
