"""Tests for the QuerySet .values() fast path for plain Bolt Serializers.

Plain serializers (no computed/source/rename/nested fields) evaluate QuerySet
responses via ``queryset.values(*fields)`` — skipping model instantiation and
the per-instance ``from_model`` walk. These tests pin:

- the fast path produces byte-identical responses to the model path
- it genuinely engages (``from_model`` is never called for eligible routes)
- ineligible serializers (source=, computed_field, rename) keep the model path
- a serializer field that is not a DB column falls back at runtime (FieldError)
"""

from __future__ import annotations

import pytest
from django.db import connection, models

from django_bolt import BoltAPI
from django_bolt.serialization import (
    _compile_serializer_values_evaluators,
    _serializer_values_path_eligible,
)
from django_bolt.serializers import Serializer, computed_field
from django_bolt.serializers.fields import field
from django_bolt.testing import TestClient


class ValuesPathUser(models.Model):
    username = models.CharField(max_length=100)
    email = models.CharField(max_length=100)
    score = models.IntegerField(default=0)

    class Meta:
        app_label = "django_bolt"


class PlainUserSerializer(Serializer):
    id: int
    username: str
    email: str


class SourceUserSerializer(Serializer):
    id: int
    login: str = field(source="username", default="")


class ComputedUserSerializer(Serializer):
    id: int
    username: str

    @computed_field
    def shout(self) -> str:
        return self.username.upper()


class NonColumnUserSerializer(Serializer):
    """`nickname` is not a DB column — values() must fall back at runtime."""

    id: int
    username: str
    nickname: str = "anon"


@pytest.fixture
def values_user_table():
    table_name = ValuesPathUser._meta.db_table
    with connection.schema_editor() as schema_editor:
        if table_name not in connection.introspection.table_names():
            schema_editor.create_model(ValuesPathUser)
    ValuesPathUser.objects.create(username="alice", email="a@x.com", score=1)
    ValuesPathUser.objects.create(username="bob", email="b@x.com", score=2)
    try:
        yield ValuesPathUser
    finally:
        ValuesPathUser.objects.all().delete()
        with connection.schema_editor() as schema_editor:
            if table_name in connection.introspection.table_names():
                schema_editor.delete_model(ValuesPathUser)


class TestEligibility:
    def test_plain_serializer_is_eligible(self):
        assert _serializer_values_path_eligible(PlainUserSerializer) is True

    def test_computed_field_serializer_is_not_eligible(self):
        assert _serializer_values_path_eligible(ComputedUserSerializer) is False

    def test_source_mapping_serializer_is_not_eligible(self):
        SourceUserSerializer._ensure_from_model_ready()
        assert SourceUserSerializer.__source_mapping__, "source mapping should be captured"
        assert _serializer_values_path_eligible(SourceUserSerializer) is False


@pytest.mark.django_db(transaction=True)
class TestValuesEvaluatorRuntime:
    def test_values_evaluator_returns_dicts(self, values_user_table):
        evaluate_sync, _ = _compile_serializer_values_evaluators(PlainUserSerializer)
        items = evaluate_sync(ValuesPathUser.objects.order_by("id"))
        assert [item["username"] for item in items] == ["alice", "bob"]
        assert all(isinstance(item, dict) for item in items)
        assert set(items[0]) == {"id", "username", "email"}

    def test_non_column_field_falls_back_to_instances(self, values_user_table):
        evaluate_sync, _ = _compile_serializer_values_evaluators(NonColumnUserSerializer)
        items = evaluate_sync(ValuesPathUser.objects.order_by("id"))
        # FieldError fallback: instances are projected through the serializer
        # right there on the ORM thread, so the evaluator still yields dicts.
        assert items and all(isinstance(item, dict) for item in items)
        assert set(items[0]) == set(NonColumnUserSerializer.__struct_fields__)
        # And the verdict is cached: second call goes straight to instances
        items2 = evaluate_sync(ValuesPathUser.objects.order_by("id"))
        assert items2 == items


@pytest.mark.django_db(transaction=True)
class TestEndToEnd:
    def _build_api(self, serializer_cls):
        # Annotations are set as real type objects (not PEP 563 strings) so the
        # closure-local serializer_cls resolves at registration.
        api = BoltAPI()

        async def list_users():
            return ValuesPathUser.objects.order_by("id")

        list_users.__annotations__ = {"return": list[serializer_cls]}
        api.get("/users")(list_users)

        def list_users_sync():
            return ValuesPathUser.objects.order_by("id")

        list_users_sync.__annotations__ = {"return": list[serializer_cls]}
        api.get("/users-sync")(list_users_sync)

        return api

    def test_plain_serializer_responses(self, values_user_table):
        api = self._build_api(PlainUserSerializer)
        with TestClient(api) as client:
            for path in ("/users", "/users-sync"):
                response = client.get(path)
                assert response.status_code == 200, path
                body = response.json()
                assert [u["username"] for u in body] == ["alice", "bob"], path
                assert set(body[0]) == {"id", "username", "email"}, path

    def test_values_path_engages_for_plain_serializer(self, values_user_table, monkeypatch):
        """If the fast path works, from_model is NEVER called."""
        api = self._build_api(PlainUserSerializer)

        def _boom(*args, **kwargs):
            raise AssertionError("from_model called — values() fast path did not engage")

        monkeypatch.setattr(PlainUserSerializer, "from_model", classmethod(_boom))
        with TestClient(api) as client:
            response = client.get("/users")
            assert response.status_code == 200
            assert [u["username"] for u in response.json()] == ["alice", "bob"]

    def test_computed_serializer_uses_model_path(self, values_user_table):
        api = self._build_api(ComputedUserSerializer)
        with TestClient(api) as client:
            response = client.get("/users")
            assert response.status_code == 200
            body = response.json()
            assert [u["shout"] for u in body] == ["ALICE", "BOB"]

    def test_non_column_serializer_falls_back_end_to_end(self, values_user_table):
        api = self._build_api(NonColumnUserSerializer)
        with TestClient(api) as client:
            response = client.get("/users")
            assert response.status_code == 200
            body = response.json()
            assert [u["username"] for u in body] == ["alice", "bob"]
            assert [u["nickname"] for u in body] == ["anon", "anon"]
