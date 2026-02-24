import msgspec
import pytest
from django.contrib.auth.models import User

from django_bolt import BoltAPI, ModelViewSet, ViewSet, action
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.auth.guards import AllowAny
from django_bolt.auth.jwt_utils import create_jwt_for_user
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from django_bolt.views import (
    CreateMixin,
    ListMixin,
    ReadOnlyModelViewSet,
    RetrieveMixin,
)
from tests.test_models import Article


@pytest.fixture
def api():
    return BoltAPI()


class ArticleSchema(Serializer):
    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleCreateSchema(Serializer):
    title: str
    content: str
    author: str


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, RetrieveMixin, CreateMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_viewset_auth(api, view_classes: list[type]):
    """Test ViewSet using auth and guards."""

    # Create test data
    Article.objects.create(title="Auth Article", content="Content", author="Author")

    user = User.objects.create(username="testuser")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

    with TestClient(api, use_http_layer=True) as client:
        # 1. No token provided - should fail (401)
        response = client.get("/articles")
        assert response.status_code == 401

        response = client.get("/articles/1")
        assert response.status_code == 401

        response = client.post(
            "/articles", json={"data": {"title": "New Article", "content": "New Content", "author": "New Author"}}
        )
        assert response.status_code == 401

        # 2. Provide valid token - should succeed (200)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/articles", headers=headers)
        assert response.status_code == 200
        assert len(response.json()) == 1

        article_id = response.json()[0]["id"]
        response = client.get(f"/articles/{article_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["title"] == "Auth Article"

        response = client.post(
            "/articles",
            json={"data": {"title": "New Article", "content": "New Content", "author": "New Author"}},
            headers=headers,
        )
        assert response.status_code == 201
        assert response.json()["title"] == "New Article"

        response = client.get("/articles", headers=headers)
        assert response.status_code == 200
        assert len(response.json()) == 2


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_mixed_viewsets_auth(api, view_classes: list[type]):
    """Test multiple ViewSets with different authentication configurations."""

    # No auth/guards, defaults to public
    @api.viewset("/public")
    class PublicViewSet(ListMixin, ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    @api.viewset("/private")
    class PrivateViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

    user = User.objects.create(username="private-user")
    token = create_jwt_for_user(user, secret="test-secret")

    with TestClient(api, use_http_layer=True) as client:
        # Public interface
        response = client.get("/public")
        assert response.status_code == 200

        # Private interface - no token
        response = client.get("/private")
        assert response.status_code == 401

        # Private interface - with token
        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


@pytest.mark.parametrize("view_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_async_action_auth_override(api, view_class):
    """Test action in ViewSet overriding class-level authentication configuration."""

    user = User.objects.create(username="action-user")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.viewset("/override")
    class OverrideViewSet(view_class):
        serializer_class = ArticleSchema
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

        @action(methods=["GET"], detail=False)
        async def protected(self, request):
            return {"status": "protected"}

        @action(methods=["GET"], detail=False, auth=[], guards=[AllowAny()])
        async def public(self, request):
            return {"status": "public"}

        @action(
            methods=["GET"], detail=False, auth=[APIKeyAuthentication(api_keys={"key123"})], guards=[IsAuthenticated()]
        )
        async def apikey(self, request):
            return {"status": "apikey"}

    with TestClient(api, use_http_layer=True) as client:
        # 1. Default action (inherits from class level)
        response = client.get("/override/protected")
        assert response.status_code == 401

        response = client.get("/override/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        # 2. Override to public action
        response = client.get("/override/public")
        assert response.status_code == 200
        assert response.json()["status"] == "public"

        # 3. Override to API Key authenticated action
        response = client.get("/override/apikey")
        assert response.status_code == 401

        response = client.get("/override/apikey", headers={"X-API-Key": "key123"})
        assert response.status_code == 200
        assert response.json()["status"] == "apikey"


@pytest.mark.parametrize("view_class", [ViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_sync_action_auth_override(api, view_class):
    """Test sync action in ViewSet overriding class-level authentication configuration."""

    user = User.objects.create(username="action-user")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.viewset("/override-sync")
    class OverrideViewSet(view_class):
        serializer_class = ArticleSchema
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

        @action(methods=["GET"], detail=False)
        def protected(self, request):
            return {"status": "protected"}

        @action(methods=["GET"], detail=False, auth=[], guards=[AllowAny()])
        def public(self, request):
            return {"status": "public"}

        @action(
            methods=["GET"], detail=False, auth=[APIKeyAuthentication(api_keys={"key123"})], guards=[IsAuthenticated()]
        )
        def apikey(self, request):
            return {"status": "apikey"}

    with TestClient(api, use_http_layer=True) as client:
        # 1. Default action (inherits from class level)
        response = client.get("/override-sync/protected")
        assert response.status_code == 401

        response = client.get("/override-sync/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

        # 2. Override to public action
        response = client.get("/override-sync/public")
        assert response.status_code == 200
        assert response.json()["status"] == "public"

        # 3. Override to API Key authenticated action
        response = client.get("/override-sync/apikey")
        assert response.status_code == 401

        response = client.get("/override-sync/apikey", headers={"X-API-Key": "key123"})
        assert response.status_code == 200
        assert response.json()["status"] == "apikey"
