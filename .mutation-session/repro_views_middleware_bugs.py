"""Reproductions of the suspected bugs in views.py and middleware.py.

Copy this file to python/tests/test_zz_repro.py of a worktree and run:
    uv run --project /home/user/django-bolt --no-sync python -m pytest python/tests/test_zz_repro.py -q -p no:cacheprovider -s
Each test prints the actual result. No test asserts: the output is the evidence.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, ModelViewSet, Request, ViewSet, action
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.middleware import Middleware, cors, middleware, rate_limit, skip_middleware
from django_bolt.responses import Response
from django_bolt.serializers import Serializer, field
from django_bolt.testing import TestClient
from django_bolt.views import APIView
from tests.test_models import Article


def show(label, response):
    print(f"\n{label}: {response.status_code} {response.content[:200]!r}")


def rows():
    return list(Article.objects.order_by("id").values_list("id", "title", "author"))


class ArticleSchema(Serializer):
    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ReadOnlyIdSchema(Serializer):
    title: str
    content: str
    author: str
    id: int = field(read_only=True, default=0)


class CamelRequired(Serializer, rename="camel"):
    title: str
    is_published: bool


class CamelDefault(Serializer, rename="camel"):
    title: str
    is_published: bool = False


# V1 ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_v1_the_body_can_change_the_primary_key():
    api = BoltAPI()
    mine = Article.objects.create(title="mine", content="c", author="me")
    other = Article.objects.create(title="other", content="c", author="other")

    @api.viewset("/mine")
    class MineViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def get_queryset(self):
            return (await super().get_queryset()).filter(author="me")

    @api.viewset("/ro")
    class ReadOnlyIdViewSet(MineViewSet):
        serializer_class = ReadOnlyIdSchema

    with TestClient(api, raise_server_exceptions=False) as client:
        show("PATCH the row of another author (direct)", client.patch(f"/mine/{other.id}", json={"title": "x"}))
        show("PATCH my row with the id of the other row", client.patch(f"/mine/{mine.id}", json={"id": other.id}))
        print("rows:", rows())
        show("PATCH my row with a new id", client.patch(f"/mine/{mine.id}", json={"id": 777}))
        print("rows:", rows())
        show(
            "PUT with a read_only id",
            client.put(f"/ro/{mine.id}", json={"id": other.id, "title": "put", "content": "c", "author": "me"}),
        )
        print("rows:", rows())


# V2 ---------------------------------------------------------------------------
def test_v2_viewset_decorator_guards_do_not_cover_action_routes():
    api = BoltAPI()

    @api.viewset("/dec", auth=[APIKeyAuthentication(api_keys={"k1"})], guards=[IsAuthenticated()])
    class DecViewSet(ViewSet):
        async def list(self, request):
            return [1]

        @action(methods=["GET"], detail=False)
        async def extra(self, request):
            return {"extra": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        show("GET /dec without a key", client.get("/dec"))
        show("GET /dec/extra without a key", client.get("/dec/extra"))


# V3, V4, V5 -------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_v3_v4_v5_client_errors_that_answer_500():
    api = BoltAPI()
    article = Article.objects.create(title="t", content="c", author="a")

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    @api.post("/function")
    async def function_route(data: ArticleSchema):
        return {"ok": True}

    bad = {"content": b"{bad", "headers": {"content-type": "application/json"}}
    with TestClient(api, raise_server_exceptions=False) as client:
        show("V3 GET /articles/abc", client.get("/articles/abc"))
        show("V3 DELETE /articles/abc", client.delete("/articles/abc"))
        show("V4 POST /articles, malformed JSON", client.post("/articles", **bad))
        show("V4 PUT /articles/{id}, malformed JSON", client.put(f"/articles/{article.id}", **bad))
        show("V4 PATCH /articles/{id}, malformed JSON", client.patch(f"/articles/{article.id}", **bad))
        show("V4 POST /articles, empty body", client.post("/articles"))
        show("V4 for comparison: function route, malformed JSON", client.post("/function", **bad))
        show("V5 PATCH /articles/{id}, body [1, 2]", client.patch(f"/articles/{article.id}", json=[1, 2]))
        show("V5 for comparison: PUT, body [1, 2]", client.put(f"/articles/{article.id}", json=[1, 2]))


# V6 ---------------------------------------------------------------------------
def test_v6_view_method_without_a_first_request_parameter():
    api = BoltAPI()

    @api.view("/no-request")
    class NoRequest(APIView):
        async def get(self):
            return {"ok": True}

    @api.view("/users/{user_id}")
    class RequestSecond(APIView):
        async def get(self, user_id: int, request: Request):
            return {"type_of_self_request": type(self.request).__name__}

    with TestClient(api, raise_server_exceptions=False) as client:
        show("GET /no-request", client.get("/no-request"))
        show("GET /users/5", client.get("/users/5"))


# V7 ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_v7_lookup_field_of_the_viewset_decorator():
    api = BoltAPI()
    Article.objects.create(title="t1", content="c", author="a")

    @api.viewset("/articles", lookup_field="title")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api, raise_server_exceptions=False) as client:
        show("GET /articles/t1", client.get("/articles/t1"))


# V8 ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_v8_partial_update_with_a_renamed_serializer():
    api = BoltAPI()
    article = Article.objects.create(title="t", content="c", author="a", is_published=False)

    @api.viewset("/required")
    class RequiredViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = CamelRequired

    @api.viewset("/default")
    class DefaultViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = CamelDefault

    with TestClient(api, raise_server_exceptions=False) as client:
        show("PATCH /required {title}", client.patch(f"/required/{article.id}", json={"title": "new"}))
        show("PATCH /required {isPublished}", client.patch(f"/required/{article.id}", json={"isPublished": True}))
        show("PATCH /default {isPublished: true}", client.patch(f"/default/{article.id}", json={"isPublished": True}))
        article.refresh_from_db()
        print("is_published in the database:", article.is_published)


# V9 ---------------------------------------------------------------------------
def test_v9_get_serializer_class_override_that_reads_self_action():
    api = BoltAPI()
    try:

        @api.viewset("/articles")
        class ArticleViewSet(ModelViewSet):
            queryset = Article.objects.all()

            def get_serializer_class(self, action=None):
                return CamelDefault if self.action == "list" else ArticleSchema

    except LookupError as error:
        print("\nregistration raises LookupError:", error)


# M1 ---------------------------------------------------------------------------
def test_m1_cors_ignores_a_bad_methods_headers_credentials_or_max_age():
    preflight = {
        "Origin": "https://a.com",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-a",
    }
    cases = [
        {"methods": "GET"},
        {"methods": [1]},
        {"headers": "X-A"},
        {"credentials": "yes"},
        {"max_age": "600"},
        {"max_age": -1},
    ]
    for kwargs in cases:
        api = BoltAPI()

        @api.get("/x")
        @cors(origins=["https://a.com"], **kwargs)
        async def handler():
            return {"ok": True}

        with TestClient(api) as client:
            headers = client.options("/x", headers=preflight).headers
        shown = {k: v for k, v in headers.items() if k.startswith("access-control-allow-") or k.endswith("max-age")}
        print(f"\n@cors({kwargs}):", shown)


# M2 ---------------------------------------------------------------------------
class ForbidAll(Middleware):
    async def process_request(self, request: Request) -> Response:
        return Response({"detail": "forbidden"}, status_code=403)


def test_m2_a_middleware_cannot_answer_with_a_response():
    api = BoltAPI(middleware=[ForbidAll])

    @api.get("/x")
    async def handler():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        show("GET /x", client.get("/x"))


# M3, M4, M5, M6 ---------------------------------------------------------------
RAN: list[str] = []


def recording(name):
    class Recording(Middleware):
        async def process_request(self, request):
            RAN.append(name)
            return await self.get_response(request)

    Recording.__name__ = Recording.__qualname__ = name
    return Recording


First, Second, Global = recording("First"), recording("Second"), recording("Global")


def test_m3_m4_m5_m6_decorators():
    api = BoltAPI(middleware=[Global])

    @api.get("/kwargs")
    @middleware(cors={"origins": ["https://a.com"]})
    async def kwargs_form():
        return {"ok": True}

    @api.get("/skip-class")
    @skip_middleware(Global)
    async def skip_class():
        return {"ok": True}

    @api.get("/stacked")
    @middleware(First)
    @middleware(Second)
    async def stacked():
        return {"ok": True}

    @api.get("/two-limits")
    @rate_limit(rps=1, burst=1)
    @rate_limit(rps=100)
    async def two_limits():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/kwargs", headers={"Origin": "https://a.com"})
        print("\nM3 @middleware(cors={...}):", response.headers.get("access-control-allow-origin"))
        RAN.clear()
        client.get("/skip-class")
        print("M4 @skip_middleware(Global) ran:", RAN)
        RAN.clear()
        client.get("/stacked")
        print("M5 @middleware(First) over @middleware(Second) ran:", RAN)
        statuses = [client.get("/two-limits").status_code for _ in range(4)]
        print("M6 statuses with @rate_limit(rps=1, burst=1) over @rate_limit(rps=100):", statuses)
