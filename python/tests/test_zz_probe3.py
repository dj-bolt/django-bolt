from __future__ import annotations
import tempfile, os
from django.http import HttpResponse, StreamingHttpResponse, FileResponse as DFileResponse, JsonResponse
from django_bolt import BoltAPI, Request
from django_bolt.middleware import DjangoMiddlewareStack, DjangoMiddleware
from django_bolt.testing import TestClient
from django_bolt.responses import JSON, FileResponse

class M:
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        if request.path == "/dstream":
            return StreamingHttpResponse(iter([b"a", b"b", b"c"]), content_type="text/plain")
        if request.path == "/dfile":
            return DFileResponse(open(__file__, "rb"))
        if request.path == "/json":
            return JsonResponse({"a": 1})
        if request.path == "/empty":
            r = HttpResponse(status=204); del r["Content-Type"]; return r
        if request.path == "/boom":
            raise RuntimeError("mw boom")
        r = self.get_response(request)
        return r

def test_p():
    for layout in (lambda: DjangoMiddlewareStack([M]), lambda: DjangoMiddleware(M)):
        api = BoltAPI(middleware=[layout()])
        for p in ["/dstream","/dfile","/json","/empty","/boom","/bfile","/bstream"]:
            api.get(p)(lambda: JSON({}))
        @api.get("/bfile")
        async def bf():
            return FileResponse(__file__)
        with TestClient(api, raise_server_exceptions=False) as c:
            for p in ["/dstream","/dfile","/json","/empty","/boom","/bfile"]:
                r = c.get(p)
                print("R", type(layout()).__name__, p, r.status_code, dict(r.headers), r.content[:30])
