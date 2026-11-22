---
description: Reference for django_bolt.serializers, with Serializer, field(), computed_field, field_validator, model_validator, views and model helpers.
---

# Serializers reference

Import each name on this page from `django_bolt.serializers`. For a task-based introduction, see [Serializers](../topics/serializers.md).

## Serializer

<!-- fragment -->
```python
class Serializer(msgspec.Struct, kw_only=True)
```

A `msgspec.Struct` with validators, field options, computed fields and Django model integration.

**Class options.** A subclass takes the keyword arguments of `msgspec.Struct`, for example `class ArticleOut(Serializer, rename="camel", forbid_unknown_fields=True)`. Bolt always sets `kw_only=True`. Thus each instance takes keyword arguments only, and a required field can follow a field with a default.

**Field declaration.** Each annotated class attribute is a field. The value of the attribute is the default, or a `field()` call.

A field with a `Serializer` type, or a `list` of one, is a nested serializer. `X | None` and `Annotated[X, ...]` around it are allowed. A field with a `Literal[...]` type accepts only the listed values.

**Instance creation.** A call of the class, such as `ArticleOut(id=1, title="Hello")`, runs the validators. It does not check the types or the `msgspec.Meta` constraints. `model_validate()`, `model_validate_json()` and a request body check everything.

**Errors at class creation:**

- `TypeError`: `Config` has a `validators` attribute.
- `TypeError`: a validator is a `@staticmethod`, or a `mode="after"` model validator is a `@classmethod`.
- `ValueError`: a validator has a mode other than `"before"` or `"after"`.

**Example:**

```python
from typing import Annotated

from msgspec import Meta

from django_bolt.serializers import Serializer, field


class ArticleOut(Serializer):
    id: int = field(read_only=True, default=0)
    title: Annotated[str, Meta(max_length=200)]
    tags: list[str] = field(default_factory=list)
```

### Serializer.Config

An inner class with options for the serializer. Each option is optional.

| Attribute | Type | Default | Description |
| --- | --- | --- | --- |
| `read_only` | `set[str]` | `set()` | Field names that input cannot set. Same as `field(read_only=True)`. Each field needs a default. |
| `write_only` | `set[str]` | `set()` | Field names that output leaves out. Same as `field(write_only=True)`. |
| `field_sets` | `dict[str, list[str]]` | `{}` | Named lists of field names, for `use()` and `fields()`. A list can name computed fields. |
| `annotations` | `dict[str, Any]` | `{}` | Query expressions, such as `Count("comments")`. The loading plan adds them to a QuerySet with `annotate()`. |
| `model` | model class | `None` | The model of the serializer. `create_serializer()` sets it. Bolt does not read it. |

### Serializer.model_validate()

<!-- fragment -->
```python
@classmethod
def model_validate(cls, data: dict[str, Any] | Any) -> Self
```

Validate `data` and return an instance. Bolt checks the types, the constraints and the validators. `read_only` fields get their defaults.

**Parameters:**

- `data`: a dict with the JSON names of the fields, or another object that `msgspec.convert()` accepts.

**Returns:** an instance of the class.

**Errors:**

- `RequestValidationError`: the data is not valid. For a dict, `errors()` has one entry for each field that fails. Each entry has `loc`, `msg` and `type`.
- `msgspec.ValidationError`: the data is not valid, and Bolt cannot list the errors of each field, for example when `data` is not a dict.
- `ImproperlyConfigured`: a `read_only` field has no default.

### Serializer.model_validate_json()

<!-- fragment -->
```python
@classmethod
def model_validate_json(cls, json_data: str | bytes) -> Self
```

Decode JSON text and validate it as `model_validate()` does.

**Errors:** the errors of `model_validate()`, and `msgspec.DecodeError` for text that is not JSON.

### Serializer.validate()

<!-- fragment -->
```python
def validate(self) -> Self
```

Check the types and the constraints of an instance that Python code made. Returns a new instance.

**Errors:** `msgspec.ValidationError`.

### Serializer.from_model()

<!-- fragment -->
```python
@classmethod
def from_model(cls, instance: Model, *, max_depth: int = 10) -> Self
```

Make an instance from a Django model instance. Each field reads the attribute with its name, or the dotted path of `field(source=...)`. This method runs no query.

- A `ForeignKey` or `OneToOneField` field with a type that is not a serializer gets the primary key, from `<name>_id`.
- A nested serializer field gets the related object, converted with `from_model()`.
- A `list` field of a many-to-many or reverse relation gets the prefetched rows. A `list` of a nested serializer converts each row. A `list[int]` gets the primary keys.
- A field with no attribute on the instance gets its default.

**Parameters:**

- `instance`: the model instance, or another object with the attributes.
- `max_depth`: the largest depth of nested serializers.

**Returns:** an instance of the class.

**Errors:**

- `UnloadedRelationError`: a field without a default needs a relation that the instance did not load. The message names the `select_related()` or `prefetch_related()` call to add.
- `ValueError`: the nesting is deeper than `max_depth`.

### Serializer.afrom_model()

<!-- fragment -->
```python
@classmethod
async def afrom_model(cls, instance: Model, *, max_depth: int = 10) -> Self
```

The async form of `from_model()`. It loads each relation that the instance did not load, with the async ORM. It sends one query for each missing relation.

**Errors:** `ValueError` when the nesting is deeper than `max_depth`.

### Serializer.from_models()

<!-- fragment -->
```python
@classmethod
def from_models(cls, objects: QuerySet | Iterable[Model]) -> list[Self]
```

Make one instance for each object. For a QuerySet, Bolt first applies the loading plan of the class. Bolt uses another iterable as it is.

**Errors:** the errors of `from_model()`.

### Serializer.afrom_models()

<!-- fragment -->
```python
@classmethod
async def afrom_models(cls, objects: QuerySet | Iterable[Model]) -> list[Self]
```

The async form of `from_models()`. It reads a QuerySet with the async ORM, and converts each row with `from_model()`. Thus a relation that the plan does not load raises `UnloadedRelationError`. It does not load the relation as `afrom_model()` does.

### Serializer.loading_plan()

<!-- fragment -->
```python
@classmethod
def loading_plan(cls, model_cls: type[Model]) -> LoadingPlan
```

Return the loads that the class needs from `model_cls`. Bolt applies this plan to a QuerySet in `from_models()`, `afrom_models()`, a `list[...]` response and `@paginate`.

**Returns:** a `LoadingPlan` with these attributes:

| Attribute | Type | Content |
| --- | --- | --- |
| `select_related` | `tuple[str, ...]` | The lookups for `select_related()`: each forward `ForeignKey` or `OneToOneField` that a nested field or a dotted `source` reads, and each reverse one-to-one relation. |
| `prefetch` | `tuple[str \| Prefetch, ...]` | The lookups for `prefetch_related()`: each many-to-many and reverse relation. A nested serializer gives a `Prefetch` with its own plan. |
| `annotations` | `dict[str, Any]` | `Config.annotations`. |

`LoadingPlan.apply(queryset)` returns the QuerySet with the plan. It keeps the loads that the QuerySet has, and it does not add them two times. It returns a `values()` QuerySet unchanged. On Django 6.1 and later, it also sets `fetch_mode(FETCH_PEERS)` on a QuerySet with the default fetch mode.

### Serializer.to_dict()

<!-- fragment -->
```python
def to_dict(self, *, exclude_unset: bool = False) -> dict[str, Any]
```

Return the fields as a dict, with the Python field names. `write_only` fields are in the dict. Computed fields are not. Nested serializers stay instances.

**Parameters:**

- `exclude_unset`: leave out each field with the value `msgspec.UNSET`.

### Serializer.to_model()

<!-- fragment -->
```python
def to_model(self, model_class: type[Model]) -> Model
```

Return a new, unsaved instance of `model_class` with the fields of the serializer. `read_only` fields are not set. A primary key in a `ForeignKey` or `OneToOneField` field goes to `<name>_id`.

**Errors:** the errors of the model constructor, for example `TypeError` for a field that the model does not have.

### Serializer.update_instance()

<!-- fragment -->
```python
def update_instance(self, instance: Model) -> Model
```

Set each field of the serializer on `instance`, and return `instance`. It does not save. `read_only` fields are not set. A primary key in a `ForeignKey` or `OneToOneField` field goes to `<name>_id`. For a class with `omit_defaults=True`, a field with its default value is not set.

### Serializer.dump()

<!-- fragment -->
```python
def dump(
    self,
    *,
    exclude_none: bool = False,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    by_alias: bool = False,
) -> dict[str, Any]
```

Return the output dict. Keys are the JSON names of the fields. `write_only` fields and `field(exclude=True)` fields are not in it. Computed fields are in it. Nested serializers are dumped too. Values keep their Python types, for example `datetime`.

A handler that returns a serializer sends the result of `dump()` with no arguments.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `exclude_none` | `False` | Leave out each field, also each computed field, with the value `None`. |
| `exclude_unset` | `False` | Leave out each field with the value `msgspec.UNSET`. |
| `exclude_defaults` | `False` | Leave out each field that is equal to its default. |
| `by_alias` | `False` | No effect. The keys are always the JSON names. |

**Errors:** `SerializationError` when a field holds a Django manager or a QuerySet.

### Serializer.dump_json()

<!-- fragment -->
```python
def dump_json(self, *, exclude_none=False, exclude_unset=False, exclude_defaults=False, by_alias=False) -> bytes
```

Return the result of `dump()` as JSON bytes.

### Serializer.dump_many()

<!-- fragment -->
```python
@classmethod
def dump_many(cls, instances: Iterable[Self], *, exclude_none=False, exclude_unset=False, exclude_defaults=False, by_alias=False) -> list[dict[str, Any]]
```

Return a list with the `dump()` of each instance.

### Serializer.dump_many_json()

<!-- fragment -->
```python
@classmethod
def dump_many_json(cls, instances: Iterable[Self], *, exclude_none=False, exclude_unset=False, exclude_defaults=False, by_alias=False) -> bytes
```

Return the result of `dump_many()` as JSON bytes.

### Serializer.subset()

<!-- fragment -->
```python
@classmethod
def subset(cls, *fields: str, name: str | None = None) -> type[Self]
```

Return a new `Serializer` class with only the named fields. Use it as a type. Make it at module level.

The new class keeps the type, the default and the `field()` options of each field, and the field validators of the fields. It keeps a model validator only when the subset has each attribute that the validator reads. It keeps the `rename`, `omit_defaults`, `forbid_unknown_fields` and `frozen` options. It does not keep the `tag` option. It has `Config.read_only` and `Config.write_only` limited to its fields.

The new class has a class method `from_parent(instance)`. It makes an instance of the subset from an instance of the full class.

**Parameters:**

- `*fields`: field names and computed field names. An unknown name is ignored.
- `name`: the class name. The default is `<Class>Subset_<hash>`.

### Serializer.fields()

<!-- fragment -->
```python
@classmethod
def fields(cls, field_set: str, *, name: str | None = None) -> type[Self]
```

Return `subset()` of the fields of the named field set. The default class name is `<Class><FieldSet>`, for example `UserSerializerList`.

**Errors:**

- `TypeError`: `field_set` is not a non-empty `str`.
- `ValueError`: `Config.field_sets` has no such name.

### Serializer.only(), exclude() and use()

<!-- fragment -->
```python
@classmethod
def only(cls, *fields: str) -> SerializerView[Self]

@classmethod
def exclude(cls, *fields: str) -> SerializerView[Self]

@classmethod
def use(cls, field_set: str) -> SerializerView[Self]
```

Return a `SerializerView` that limits the output fields. `only()` keeps the named fields. `exclude()` leaves out the named fields. `use()` keeps the fields of a field set. An unknown field name is ignored.

**Errors:** `use()` raises `ValueError` when `Config.field_sets` has no such name.

## SerializerView

A view of a serializer class with a limited set of output fields. `only()`, `exclude()` and `use()` return it. A view is not a type: do not use it in an annotation.

| Method | Description |
| --- | --- |
| `only(*fields)` | Return a view that keeps only the named fields of this view. |
| `exclude(*fields)` | Return a view that also leaves out the named fields. |
| `dump(instance, *, exclude_none=False, exclude_unset=False, exclude_defaults=False, by_alias=False)` | Return the output dict of `instance`. |
| `dump_many(instances, ...)` | Return a list of output dicts. |
| `dump_json(instance, ...)` | Return JSON bytes. |
| `dump_many_json(instances, ...)` | Return JSON bytes of a list. |
| `from_model(instance, **kwargs)` | Same as `Serializer.from_model()` of the class. |
| `afrom_model(instance, **kwargs)` | Same as `Serializer.afrom_model()` of the class. |

## field()

<!-- fragment -->
```python
def field(
    *,
    read_only: bool = False,
    write_only: bool = False,
    source: str | None = None,
    alias: str | None = None,
    default: Any = <no default>,
    default_factory: Callable[[], Any] | None = None,
    description: str | None = None,
    title: str | None = None,
    examples: list[Any] | None = None,
    deprecated: bool = False,
    exclude: bool = False,
    include_in_schema: bool = True,
) -> Any
```

Set the options of a field. Use the result as the value of the class attribute.

**Parameters:**

| Name | Description |
| --- | --- |
| `read_only` | Input cannot set the field. A request body, `Query()`, `Header()`, `Cookie()` and `Form()` models, `model_validate()` and `model_validate_json()` give it its default. With `forbid_unknown_fields=True`, an input value is an error. Python code and `from_model()` set it. The OpenAPI request schema leaves it out. The response schema marks it `readOnly`. |
| `write_only` | Input accepts the field. Output leaves it out. |
| `source` | The attribute that `from_model()` reads. A dotted path, such as `"author.email"`, follows relations. |
| `alias` | The JSON name of the field, for input and output. The input does not accept the Python name. It wins over the `rename` option of the class. |
| `default` | The default value. With no default and no `default_factory`, the field is required. |
| `default_factory` | A function with no arguments that makes the default. |
| `description`, `title`, `examples` | The fields of the property in the OpenAPI schema. |
| `deprecated` | Mark the property as deprecated in the OpenAPI schema. |
| `exclude` | Output always leaves the field out. |
| `include_in_schema` | `False` leaves the property out of the OpenAPI schema and out of `required`. The field still validates input and is in the output. |

**Errors:** `ImproperlyConfigured` at the first input, for a `read_only` field with no default.

`field()` has no constraints. Use `Annotated[T, Meta(...)]`. `FieldConfig` is the frozen dataclass that holds these options.

## computed_field()

<!-- fragment -->
```python
def computed_field(
    func=None,
    *,
    alias: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
    include_in_schema: bool = True,
    title: str | None = None,
    examples: list[Any] | None = None,
)
```

Decorate a method with no arguments to make an output field. The field name is the method name, or `alias`. The field type is the return type of the method. Use it as `@computed_field` or `@computed_field(...)`.

Bolt calls the method at each `dump()`. Input does not accept the field. The OpenAPI response schema has a required, `readOnly` property for it. A request schema does not have it.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `alias` | `None` | The JSON name of the field. |
| `description` | the docstring of the method | The `description` of the property. |
| `deprecated` | `False` | Mark the property as deprecated. |
| `include_in_schema` | `True` | `False` leaves the property out of the schema. `dump()` still returns the field. |
| `title` | `None` | The `title` of the property. |
| `examples` | `None` | The `examples` of the property. |

## field_validator()

<!-- fragment -->
```python
def field_validator(field_name: str, mode: Literal["before", "after"] = "after")
```

Decorate a method that validates one field. Bolt calls it as `method(cls, value)`. The method can also have `@classmethod` under this decorator.

**Parameters:**

- `field_name`: the Python name of the field.
- `mode`:
    - `"after"`: the method gets the value after the type check. A return value other than `None` replaces the value.
    - `"before"`: the method gets the raw input value. Bolt uses the return value, also `None`, and then checks its type. It runs only for a field that the input has.

**Errors:** raise `ValueError` or `TypeError` in the method to reject the value. A request gets `422` with `"type": "value_error"` and the `loc` of the field. Bolt collects the errors of all `"after"` validators.

Order: the `"before"` validators run before the type check, the last one defined first. The `"after"` validators run after the type check, in definition order, the validators of a parent class first.

## model_validator()

<!-- fragment -->
```python
def model_validator(func=None, mode: Literal["before", "after"] = "after")
```

Decorate a method that validates the whole object. Use it as `@model_validator` or `@model_validator(mode=...)`.

**Parameters:**

- `mode`:
    - `"after"`: a method that gets the instance after the field validators. Its return value is ignored.
    - `"before"`: a `@classmethod` that gets the raw input, usually a dict, before any field validation. Return the data to validate.

**Errors:** raise `ValueError` or `TypeError` to reject the data. A request gets `422` with `"loc": ["body"]` and `"type": "value_error"`.

## create_serializer()

<!-- fragment -->
```python
def create_serializer(
    model: type[Model],
    *,
    fields: list[str] | None = None,
    exclude: list[str] | None = None,
    write_only: set[str] | None = None,
    read_only: set[str] | None = None,
    serializer_name: str | None = None,
) -> type[Serializer]
```

Return a new `Serializer` class with a field for each concrete model field. The [guide](../topics/serializers.md#create-a-serializer-from-a-model) lists the type of each model field.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `model` | required | The model class. |
| `fields` | `None` | The field names, in output order. `None` takes each concrete field. An unknown name is ignored. |
| `exclude` | `None` | Field names to leave out. |
| `write_only` | `None` | Becomes `Config.write_only`. |
| `read_only` | `None` | Becomes `Config.read_only`. |
| `serializer_name` | `"<Model>Serializer"` | The class name. |

The fields have no defaults. `ManyToManyField` fields are skipped.

## create_serializer_set()

<!-- fragment -->
```python
def create_serializer_set(
    model: type[Model],
    *,
    create_fields: list[str] | None = None,
    update_fields: list[str] | None = None,
    public_fields: list[str] | None = None,
    write_only: set[str] | None = None,
    read_only: set[str] | None = None,
) -> tuple[type[Serializer], type[Serializer], type[Serializer]]
```

Return three classes from `create_serializer()`: `<Model>Create` with `create_fields`, `<Model>Update` with `update_fields`, and `<Model>Public` with `public_fields`. `write_only` applies to the create and update classes. `read_only` applies to the public class.

## Field types

`django_bolt.serializers` exports `Annotated` types with constraints, and `Meta` from msgspec. The [guide](../topics/serializers.md#use-the-built-in-field-types) lists each type.

## Exceptions

| Exception | Module | Raised when |
| --- | --- | --- |
| `RequestValidationError` | `django_bolt.exceptions` | `model_validate()` or `model_validate_json()` gets data that is not valid. |
| `SerializationError` | `django_bolt.exceptions` | `dump()` finds a manager or a QuerySet in a field. A subclass of `ValueError`. |
| `UnloadedRelationError` | `django_bolt.exceptions` | `from_model()` needs a relation that is not loaded. A subclass of `SerializationError`. |
| `ImproperlyConfigured` | `django.core.exceptions` | Input reaches a `read_only` field with no default. |
