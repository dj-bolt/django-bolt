from typing import Annotated

import pytest
from django.contrib.auth.models import User

from django_bolt import (
    BoltAPI,
    CreateMixin,
    DestroyMixin,
    ModelViewSet,
    PartialUpdateMixin,
    ReadOnlyModelViewSet,
    UpdateMixin,
    ViewSet,
    action,
)
from django_bolt.params import Depends
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from django_bolt.views import WritePayload
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


di_user = {"id": 1, "name": "John"}


def get_user(request) -> dict:
    return di_user.copy()


di_settings = {"theme": "dark", "language": "en"}


def get_settings(request) -> dict:
    return di_settings.copy()


@pytest.mark.parametrize(
    "view_classes",
    [
        (CreateMixin, UpdateMixin, PartialUpdateMixin, DestroyMixin, ViewSet),
        (CreateMixin, UpdateMixin, PartialUpdateMixin, DestroyMixin, ReadOnlyModelViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_viewset_custom_curd_depends(api, view_classes: list[type]):
    @api.viewset("/dep")
    class DepViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

        async def list(
            self,
            request,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return Article.objects.all()

        async def retrieve(
            self,
            request,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return await Article.objects.aget(id=request.params["pk"])

        async def create(
            self,
            request,
            data: WritePayload,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return await super().create(request, data=data)

        async def update(
            self,
            request,
            data: WritePayload,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return await super().update(request, data=data)

        async def partial_update(
            self,
            request,
            data: WritePayload,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return await super().partial_update(request, data=data)

        async def destroy(
            self,
            request,
            user: Annotated[User, Depends(get_user)],
            settings: Annotated[dict, Depends(get_settings)] = None,
        ):
            assert user == di_user
            assert settings == di_settings
            return await super().destroy(request)

    with TestClient(api) as client:
        # Test List
        r = client.get("/dep")
        assert r.status_code == 200

        # Create an article first to test other methods
        article_data = {"data": {"title": "Test Article", "content": "Test Content", "author": "John"}}
        r = client.post("/dep", json=article_data)
        assert r.status_code == 201
        article_id = r.json()["id"]

        # Test Retrieve
        r = client.get(f"/dep/{article_id}")
        assert r.status_code == 200
        assert r.json()["title"] == "Test Article"

        # Test Update
        updated_data = {"data": {"title": "Updated Article", "content": "Updated Content", "author": "John"}}
        r = client.put(f"/dep/{article_id}", json=updated_data)
        assert r.status_code == 200
        assert r.json()["title"] == "Updated Article"

        # Test Partial Update
        partial_data = {"data": {"title": "Partially Updated"}}
        r = client.patch(f"/dep/{article_id}", json=partial_data)
        assert r.status_code == 200
        assert r.json()["title"] == "Partially Updated"

        # Test Destroy
        r = client.delete(f"/dep/{article_id}")
        assert r.status_code == 204


@pytest.mark.parametrize("view_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_action_depends(api, view_class: type):
    @api.viewset("/custom")
    class CustomDepViewSet(view_class):
        serializer_class = ArticleSchema

        @action(["GET"], detail=False)
        async def info(self, request, user: Annotated[dict, Depends(get_user)]):
            return user

        @action(["GET"], detail=False)
        def settings(self, request, value: Annotated[int, Depends(get_settings)]):
            return value

    with TestClient(api, use_http_layer=True) as client:
        r = client.get("/custom/info")
        assert r.status_code == 200
        assert r.json() == di_user

        r = client.get("/custom/settings")
        assert r.status_code == 200
        assert r.json() == di_settings
