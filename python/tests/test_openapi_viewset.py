"""
OpenAPI schema generation for ModelViewSet routes.

Covers:
- Path parameters declared in the route ({pk}) are surfaced in the spec even
  when the underlying mixin method does not bind them as a function parameter.
- POST /create operations document the request body using create_serializer_class.
- PUT/PATCH operations honor update_serializer_class precedence over
  create_serializer_class and serializer_class.
- Read actions (list/retrieve/destroy) do not get a request body.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, ModelViewSet, ViewSet
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator, _extract_path_param_names
from django_bolt.serializers import Serializer
from django_bolt.views import APIView, CreateMixin, PartialUpdateMixin, UpdateMixin
from tests.test_models import Article


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


class ArticleUpdateSchema(Serializer):
    title: str
    content: str


def _generate(api: BoltAPI):
    config = OpenAPIConfig(title="t", version="1.0.0")
    return SchemaGenerator(api, config).generate()


def test_extract_path_param_names_basic():
    assert _extract_path_param_names("/articles/{pk}") == ["pk"]
    assert _extract_path_param_names("/a/{x}/b/{y}") == ["x", "y"]
    assert _extract_path_param_names("/no/params") == []


def test_viewset_path_param_appears_in_spec():
    """ModelViewSet detail routes ({pk}) must surface as path parameters even
    though retrieve/update/destroy mixins do not bind pk as a function arg."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    schema = _generate(api)

    # Detail path is registered as /articles/{pk}
    assert "/articles/{pk}" in schema.paths
    detail = schema.paths["/articles/{pk}"]

    for op in (detail.get, detail.put, detail.patch, detail.delete):
        assert op is not None
        param_names = {p.name for p in (op.parameters or []) if p.param_in == "path"}
        assert "pk" in param_names, f"pk missing from {op.operation_id}"


def test_create_uses_create_serializer_class():
    """POST should document its body using create_serializer_class when set."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

    schema = _generate(api)

    post_op = schema.paths["/articles"].post
    assert post_op is not None
    assert post_op.request_body is not None
    media = post_op.request_body.content["application/json"]
    # The schema is registered as a component reference
    ref = media.schema.ref if hasattr(media.schema, "ref") else None
    assert ref == "#/components/schemas/ArticleCreateSchema"


def test_update_prefers_update_serializer_class():
    """PUT must use update_serializer_class even when create_serializer_class
    is also defined. Regression guard for the original PR's incorrect override
    that always used create_serializer_class for non-create actions."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema
        update_serializer_class = ArticleUpdateSchema

    schema = _generate(api)
    detail = schema.paths["/articles/{pk}"]

    for op in (detail.put, detail.patch):
        assert op is not None
        assert op.request_body is not None
        media = op.request_body.content["application/json"]
        ref = media.schema.ref if hasattr(media.schema, "ref") else None
        assert ref == "#/components/schemas/ArticleUpdateSchema", (
            f"{op.operation_id} should use update_serializer_class, got {ref}"
        )


def test_update_falls_back_to_create_then_serializer_class():
    """When update_serializer_class is unset, PUT/PATCH fall back to
    create_serializer_class, then serializer_class. Mirrors
    ViewSet._get_default_serializer_class precedence."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

    schema = _generate(api)
    put_op = schema.paths["/articles/{pk}"].put
    assert put_op is not None
    media = put_op.request_body.content["application/json"]
    ref = media.schema.ref if hasattr(media.schema, "ref") else None
    assert ref == "#/components/schemas/ArticleCreateSchema"


@pytest.mark.parametrize(
    ("attribute", "mixins", "operations"),
    [
        (
            "serializer_class",
            (CreateMixin, UpdateMixin, PartialUpdateMixin),
            [("/articles", "post"), ("/articles/{pk}", "put"), ("/articles/{pk}", "patch")],
        ),
        ("create_serializer_class", (CreateMixin,), [("/articles", "post")]),
        (
            "update_serializer_class",
            (UpdateMixin, PartialUpdateMixin),
            [("/articles/{pk}", "put"), ("/articles/{pk}", "patch")],
        ),
    ],
)
def test_one_serializer_attribute_documents_the_body_of_the_write_actions(attribute, mixins, operations):
    """Each of the three serializer attributes, with no other one, gives the write actions their body."""
    api = BoltAPI()
    viewset = type(
        "ArticleViewSet", (*mixins, ViewSet), {"queryset": Article.objects.all(), attribute: ArticleUpdateSchema}
    )
    api.viewset("/articles")(viewset)

    schema = _generate(api)

    for path, method in operations:
        media = getattr(schema.paths[path], method).request_body.content["application/json"]
        assert media.schema.ref == "#/components/schemas/ArticleUpdateSchema", (method, path)


def test_api_view_operation_takes_its_id_and_its_text_from_the_method():
    """The operation id holds the class and the method. The docstring gives the summary and the description."""
    api = BoltAPI()

    @api.view("/hello")
    class HelloView(APIView):
        async def get(self, request) -> dict:
            """Say hello.

            Answer each caller with one greeting.
            """
            return {"message": "Hello"}

    operation = _generate(api).paths["/hello"].get

    assert operation.operation_id == "get_HelloView.get"
    assert (operation.summary, operation.description) == ("Say hello.", "Answer each caller with one greeting.")


def test_read_actions_have_no_request_body():
    """GET/DELETE routes must not have a request body."""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

    schema = _generate(api)
    list_op = schema.paths["/articles"].get
    retrieve_op = schema.paths["/articles/{pk}"].get
    destroy_op = schema.paths["/articles/{pk}"].delete

    assert list_op.request_body is None
    assert retrieve_op.request_body is None
    assert destroy_op.request_body is None


def test_path_param_fallback_does_not_duplicate_typed_param():
    """If a handler explicitly declares a typed path param, the fallback must
    not add a duplicate untyped one."""
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item(item_id: int) -> dict:
        return {"item_id": item_id}

    schema = _generate(api)
    op = schema.paths["/items/{item_id}"].get
    path_params = [p for p in (op.parameters or []) if p.param_in == "path"]
    assert len(path_params) == 1
    assert path_params[0].name == "item_id"
    # The typed annotation drives the schema, not the string fallback
    assert path_params[0].schema.type == "integer"
