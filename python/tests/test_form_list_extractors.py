"""Unit tests for form list[T] field extraction helpers.

Tests the new helper functions added in PR "fix: bind repeated form keys to
list[T] Form() struct fields":
  - _is_sequence_field
  - _collect_sequence_field_names
  - Scalar-to-list wrapping logic inside _create_param_struct_extractor
"""
from __future__ import annotations

from typing import Optional

import msgspec

from django_bolt._kwargs.extractors import (
    _collect_sequence_field_names,
    _create_param_struct_extractor,
    _is_sequence_field,
)
from django_bolt.datastructures import UploadFile


# ---------------------------------------------------------------------------
# _is_sequence_field
# ---------------------------------------------------------------------------


class TestIsSequenceField:
    """Tests for _is_sequence_field — identifies list/set/frozenset/tuple fields."""

    def test_list_str(self):
        assert _is_sequence_field(list[str]) is True

    def test_list_int(self):
        assert _is_sequence_field(list[int]) is True

    def test_set_str(self):
        assert _is_sequence_field(set[str]) is True

    def test_frozenset_str(self):
        assert _is_sequence_field(frozenset[str]) is True

    def test_tuple_str_ellipsis(self):
        assert _is_sequence_field(tuple[str, ...]) is True

    def test_optional_list_str(self):
        """Optional[list[T]] should still be recognised after unwrapping."""
        assert _is_sequence_field(Optional[list[str]]) is True

    def test_optional_list_int(self):
        assert _is_sequence_field(Optional[list[int]]) is True

    def test_plain_str_is_not_sequence(self):
        assert _is_sequence_field(str) is False

    def test_plain_int_is_not_sequence(self):
        assert _is_sequence_field(int) is False

    def test_plain_bool_is_not_sequence(self):
        assert _is_sequence_field(bool) is False

    def test_dict_is_not_sequence(self):
        # dict is not in _SEQUENCE_ORIGINS
        assert _is_sequence_field(dict[str, str]) is False

    def test_optional_str_is_not_sequence(self):
        assert _is_sequence_field(Optional[str]) is False

    def test_optional_int_is_not_sequence(self):
        assert _is_sequence_field(Optional[int]) is False


# ---------------------------------------------------------------------------
# _collect_sequence_field_names
# ---------------------------------------------------------------------------


class TestCollectSequenceFieldNames:
    """Tests for _collect_sequence_field_names — returns wire-side names of
    sequence-typed struct fields."""

    def test_no_sequence_fields_returns_empty_tuple(self):
        class Params(msgspec.Struct):
            name: str
            age: int

        assert _collect_sequence_field_names(Params) == ()

    def test_single_list_field(self):
        class Params(msgspec.Struct):
            name: str
            tags: list[str] = []

        names = _collect_sequence_field_names(Params)
        assert names == ("tags",)

    def test_multiple_list_fields(self):
        class Params(msgspec.Struct):
            name: str
            tags: list[str] = []
            counts: list[int] = []

        names = _collect_sequence_field_names(Params)
        assert set(names) == {"tags", "counts"}
        assert len(names) == 2

    def test_set_field_included(self):
        class Params(msgspec.Struct):
            items: set[str] = msgspec.field(default_factory=set)

        names = _collect_sequence_field_names(Params)
        assert "items" in names

    def test_frozenset_field_included(self):
        class Params(msgspec.Struct):
            ids: frozenset[int] = frozenset()

        names = _collect_sequence_field_names(Params)
        assert "ids" in names

    def test_tuple_field_included(self):
        class Params(msgspec.Struct):
            coords: tuple[float, ...] = ()

        names = _collect_sequence_field_names(Params)
        assert "coords" in names

    def test_optional_list_field_included(self):
        """Optional[list[T]] fields are sequence-typed and must be included."""

        class Params(msgspec.Struct):
            name: str
            tags: Optional[list[str]] = None

        names = _collect_sequence_field_names(Params)
        assert "tags" in names

    def test_returns_tuple_type(self):
        class Params(msgspec.Struct):
            tags: list[str] = []

        result = _collect_sequence_field_names(Params)
        assert isinstance(result, tuple)

    def test_encode_name_used_when_present(self):
        """When a field has a rename (encode_name differs from name), the
        wire-side encode_name must appear, not the Python attribute name."""

        class Params(msgspec.Struct, rename="camel"):
            tag_list: list[str] = []

        names = _collect_sequence_field_names(Params)
        # With camel rename, 'tag_list' → 'tagList'
        assert "tagList" in names
        assert "tag_list" not in names

    def test_mixed_sequence_and_scalar_fields(self):
        class Params(msgspec.Struct):
            username: str
            age: int
            active: bool = True
            tags: list[str] = []
            scores: list[float] = []

        names = _collect_sequence_field_names(Params)
        assert set(names) == {"tags", "scores"}
        # Scalar fields are not included
        assert "username" not in names
        assert "age" not in names
        assert "active" not in names


# ---------------------------------------------------------------------------
# Scalar-to-list wrapping logic in _create_param_struct_extractor
# ---------------------------------------------------------------------------


class TestCreateParamStructExtractorScalarWrapping:
    """Tests for the scalar-to-list wrapping that happens inside the
    extractor returned by _create_param_struct_extractor."""

    def _make_extractor(self, struct_type):
        """Helper: build the extractor for struct_type."""
        import inspect
        return _create_param_struct_extractor(struct_type, inspect.Parameter.empty, "form")

    def test_scalar_value_wrapped_to_list(self):
        """A single scalar occurrence of a list[T] field is wrapped to [value]."""

        class Form(msgspec.Struct):
            name: str
            tags: list[str] = []

        extractor = self._make_extractor(Form)
        result = extractor({"name": "alice", "tags": "solo"})

        assert result.name == "alice"
        assert result.tags == ["solo"]

    def test_list_value_not_double_wrapped(self):
        """A value already a list must not be wrapped again."""

        class Form(msgspec.Struct):
            tags: list[str] = []

        extractor = self._make_extractor(Form)
        result = extractor({"tags": ["a", "b", "c"]})

        assert result.tags == ["a", "b", "c"]

    def test_missing_field_uses_struct_default(self):
        """Absent list field falls back to the struct default."""

        class Form(msgspec.Struct):
            name: str
            tags: list[str] = []

        extractor = self._make_extractor(Form)
        result = extractor({"name": "dave"})

        assert result.tags == []

    def test_none_value_not_wrapped(self):
        """None for an Optional[list[T]] field should not be wrapped to [None]."""

        class Form(msgspec.Struct):
            name: str
            tags: Optional[list[str]] = None

        extractor = self._make_extractor(Form)
        result = extractor({"name": "eve", "tags": None})

        assert result.tags is None

    def test_no_list_fields_no_copy_of_param_map(self):
        """When there are no sequence fields, param_map is passed as-is
        (the extractor must not fail with a copy-free fast path)."""

        class Form(msgspec.Struct):
            name: str
            age: int

        extractor = self._make_extractor(Form)
        result = extractor({"name": "frank", "age": 42})

        assert result.name == "frank"
        assert result.age == 42

    def test_multiple_list_fields_all_wrapped(self):
        """When multiple list fields arrive as scalars, all are wrapped."""

        class Form(msgspec.Struct):
            name: str
            tags: list[str] = []
            counts: list[int] = []

        extractor = self._make_extractor(Form)
        result = extractor({"name": "grace", "tags": "only-tag", "counts": "7"})

        assert result.tags == ["only-tag"]
        assert result.counts == [7]

    def test_one_list_field_scalar_other_already_list(self):
        """Only the scalar occurrence is wrapped; the list one is left alone."""

        class Form(msgspec.Struct):
            tags: list[str] = []
            scores: list[int] = []

        extractor = self._make_extractor(Form)
        result = extractor({"tags": "solo", "scores": [10, 20]})

        assert result.tags == ["solo"]
        assert result.scores == [10, 20]

    def test_int_list_coercion_from_string(self):
        """list[int] — string scalar value must be coerced to int inside list."""

        class Form(msgspec.Struct):
            counts: list[int] = []

        extractor = self._make_extractor(Form)
        result = extractor({"counts": "42"})

        assert result.counts == [42]

    def test_empty_list_preserved(self):
        """An explicit empty list must not be replaced by the struct default."""

        class Form(msgspec.Struct):
            tags: list[str] = msgspec.field(default_factory=lambda: ["default"])

        extractor = self._make_extractor(Form)
        result = extractor({"tags": []})

        assert result.tags == []

    def test_extractor_has_needs_files_map_false(self):
        """Non-file extractors expose needs_files_map=False for dispatcher."""

        class Form(msgspec.Struct):
            tags: list[str] = []

        extractor = self._make_extractor(Form)
        assert extractor.needs_files_map is False

    def test_scalar_wrapping_with_upload_file_field(self):
        """needs_files_map=True branch still applies sequence scalar wrapping."""

        class Form(msgspec.Struct):
            tags: list[str] = []
            upload: Optional[UploadFile] = None

        extractor = self._make_extractor(Form)
        assert extractor.needs_files_map is True

        file_info = {
            "filename": "note.txt",
            "content": b"hello",
            "content_type": "text/plain",
            "size": 5,
        }
        result = extractor({"tags": "solo"}, {"upload": file_info})

        assert result.tags == ["solo"]
        assert isinstance(result.upload, UploadFile)
        assert result.upload.filename == "note.txt"