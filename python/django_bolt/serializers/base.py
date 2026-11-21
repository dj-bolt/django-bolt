"""Base Serializer class extending msgspec.Struct with validation and Django integration."""

# The feature-heavy dump path still runs in Python. If we push serializer
# execution further into Rust later, this module is the main place where a
# dump-plan executor could replace slow-path field iteration.

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from weakref import WeakKeyDictionary

if sys.version_info >= (3, 14):
    import annotationlib

import msgspec
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db.models import Choices
from django.db.models import Model as DjangoModel
from django.db.models.manager import BaseManager
from django.db.models.query import QuerySet
from msgspec import ValidationError as MsgspecValidationError
from msgspec import structs as msgspec_structs

from django_bolt import _json
from django_bolt._response_dump import compile_field_dumpers
from django_bolt._serializer_before import PLAN_NOT_BUILT, RAW_VALIDATED, before_plan, run_before_validators
from django_bolt._serializer_input import INPUT_MODE, input_has_read_only_fields
from django_bolt.error_handlers import msgspec_validation_error_to_dict
from django_bolt.exceptions import (
    RequestValidationError,
    SerializationError,
    UnloadedRelationError,
)

from .decorators import (
    ComputedFieldConfig,
    collect_computed_fields,
    collect_field_validators,
    collect_model_validators,
)
from .fields import FieldConfig, _FieldMarker, field
from .loading import LoadingPlan, build_loading_plan
from .nested import resolve_nested_config, validate_nested_field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from django.db.models import Model

T = TypeVar("T", bound="Serializer")
_MISSING = object()
_USE_DEFAULT = object()
_STRUCT_META = type(msgspec.Struct)
_MODEL_VALUE_NOOP_TYPES = (str, int, float, bool, bytes, type(None))
# msgspec keeps a mutable default ([], {} or default_factory=...) as an object of this type.
_MSGSPEC_FACTORY = type(msgspec.defstruct("_FactoryProbe", [("value", list, [])]).__struct_defaults__[0])
# The type of the result of ``msgspec.field()``.
_MSGSPEC_FIELD = type(msgspec.field())


@dataclass(frozen=True)
class _ModelOutputNested:
    """Cached nested output metadata for from_model()/afrom_model()."""

    serializer_class: type[Serializer]
    many: bool


@dataclass(frozen=True)
class _ModelFieldSpec:
    """Cached model extraction metadata for a serializer field."""

    field_name: str
    source: str | None
    nested: _ModelOutputNested | None
    has_default: bool


@dataclass(frozen=True)
class _DumpFieldSpec:
    """Cached dump metadata for one struct field."""

    field_name: str
    output_key: str
    nested: _ModelOutputNested | None
    default_value: Any = _MISSING
    # Dumps each Serializer in a field of another type, for example a Struct or a dict.
    # dump() reads ``nested`` first, so a nested field never uses it.
    dumper: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class _ComputedDumpSpec:
    """Cached dump metadata for one computed field."""

    field_name: str
    method_name: str


@dataclass(frozen=True)
class _DjangoRelationInfo:
    """Lightweight cached description of a Django relation accessor."""

    name: str
    kind: Literal["forward_one", "reverse_one", "many"]
    cache_name: str
    related_model: type[DjangoModel] | None
    attname: str | None = None
    reverse_query_attname: str | None = None


@dataclass(frozen=True)
class _UnloadedRelation:
    """Marker describing a relation path that would require ORM I/O."""

    source: str
    relation_name: str
    relation_kind: Literal["forward_one", "reverse_one", "many"]


type _RelationTaskCache = dict[tuple[int, str], asyncio.Task[Any]]
_DJANGO_RELATION_CACHE: WeakKeyDictionary[type, dict[str, _DjangoRelationInfo | None]] = WeakKeyDictionary()
# A plain dict: its lookup runs in each to_model() call, and a weak dictionary is slower.
_RELATION_ATTNAMES: dict[type, dict[str, str]] = {}


def _relation_attnames(model_class: type[Model]) -> dict[str, str]:
    """Map each ForeignKey and OneToOneField name of the model to its column attribute.

    For example ``author`` maps to ``author_id``. Django takes a model instance
    on the first name and a primary key on the second. The callers read
    ``_RELATION_ATTNAMES`` first, so the map is made one time for each model.
    """
    names = _RELATION_ATTNAMES[model_class] = {
        field.name: field.attname
        for field in model_class._meta.concrete_fields
        if field.is_relation and field.attname != field.name
    }
    return names


def _iter_field_defaults(cls: type) -> list[tuple[str, Any]]:
    """
    Iterate over (field_name, default_value) pairs for a msgspec.Struct class.

    msgspec stores defaults aligned from the END of the fields list, so we need
    to compute the correct field index for each default.

    Args:
        cls: A msgspec.Struct subclass

    Returns:
        List of (field_name, default_value) tuples
    """
    defaults = getattr(cls, "__struct_defaults__", ())
    fields = getattr(cls, "__struct_fields__", ())
    num_fields = len(fields)
    num_defaults = len(defaults)

    result = []
    for i, default_val in enumerate(defaults):
        field_idx = num_fields - num_defaults + i
        if 0 <= field_idx < num_fields:
            result.append((fields[field_idx], default_val))
    return result


def _names_in_code(func: Any) -> frozenset[str]:
    """Return each name that the code of ``func`` uses, such as ``low`` for ``self.low``.

    The names of a lambda or of a nested function in ``func`` count too. A
    callable with no Python code gives no name.
    """
    code = getattr(inspect.unwrap(func), "__code__", None)
    names: set[str] = set()
    pending = [code] if code is not None else []
    while pending:
        current = pending.pop()
        names.update(current.co_names)
        pending.extend(const for const in current.co_consts if inspect.iscode(const))
    return frozenset(names)


def _build_rename_map(cls: type[msgspec.Struct]) -> dict[str, str]:
    """
    Build field name -> encoded name mapping for msgspec rename support.

    This extracts the rename mapping (e.g., {"user_name": "userName"} for rename="camel",
    or {"user_name": "u"} for field(alias="u")). It reads the names that msgspec
    keeps on the class, so it does not resolve the annotations.

    Args:
        cls: A msgspec.Struct subclass

    Returns:
        Dict mapping Python field names to their encoded names.
        Empty dict if no field has another name in JSON.
    """
    return {
        name: encode_name
        for name, encode_name in zip(cls.__struct_fields__, cls.__struct_encode_fields__, strict=True)
        if name != encode_name
    }


def _read_only_input_default(default: Any) -> tuple[Any, Any, bool]:
    """
    How input mode fills a ``read_only`` field: ``(default, factory, is_marker)``.

    ``factory`` makes the value when it is not None. Without it, ``default`` is
    the value, and ``msgspec.NODEFAULT`` tells that the field has no default.
    ``is_marker`` tells that the struct default is a ``field()`` marker.
    """
    if isinstance(default, _FieldMarker):
        config = default.config
        if config.default_factory is not None:
            return msgspec.NODEFAULT, config.default_factory, True
        return (config.default if config.has_default() else msgspec.NODEFAULT), None, True
    if type(default) is _MSGSPEC_FACTORY:
        return msgspec.NODEFAULT, default.factory, False
    return default, None, False


def _resolve_dump_default(default_value: Any) -> Any:
    """Resolve msgspec/_FieldMarker defaults into their runtime value."""
    if isinstance(default_value, _FieldMarker) and default_value.config.has_default():
        return default_value.config.get_default()
    if isinstance(default_value, _MSGSPEC_FACTORY):
        return default_value.factory()
    return default_value


def _get_model_output_nested(field_type: Any) -> _ModelOutputNested | None:
    """Infer nested output behavior from the type hint."""
    nested_config = resolve_nested_config(field_type)
    if nested_config is None:
        return None

    return _ModelOutputNested(
        serializer_class=nested_config.serializer_class,
        many=nested_config.many,
    )


def _dump_nested_one(value: Any) -> Any:
    return value.dump() if isinstance(value, Serializer) else value


def _dump_nested_many(value: Any) -> Any:
    if isinstance(value, list) and value and isinstance(value[0], Serializer):
        return [item.dump() for item in value]
    return value


def _compile_dump_function(cls: type[Serializer]) -> Callable[[Any], dict[str, Any]]:
    """Return a function that gives the dump() of ``cls`` with no options.

    The function builds the dict in one expression, as ``_dump_impl`` does
    in a loop. This is about 6 times faster. The source holds only the field
    and method names of the class, which are identifiers, and literals.
    """
    specs = cls._dump_field_specs
    namespace: dict[str, Any] = {"_one": _dump_nested_one, "_many": _dump_nested_many, "_tag": cls.__tag_value__}
    items = [f"{cls.__tag_field__!r}: _tag"] if cls.__tag_value__ is not None else []
    for index, spec in enumerate(specs):
        value = f"s.{spec.field_name}"
        if spec.nested is not None:
            value = f"{'_many' if spec.nested.many else '_one'}({value})"
        elif spec.dumper is not None:
            namespace[f"_dump{index}"] = spec.dumper
            value = f"_dump{index}({value})"
        items.append(f"{spec.output_key!r}: {value}")
    items += [f"{spec.field_name!r}: s.{spec.method_name}()" for spec in cls._computed_dump_specs]
    # A manager or a QuerySet in a dumped field fails, as in _dump_impl. A
    # relation can leak into a field of an untyped container, or into a field
    # that can hold a Serializer. Each nested field can hold a Serializer.
    checked = tuple(
        spec.field_name
        for spec in specs
        if spec.dumper is not None or spec.field_name in cls.__orm_state_check_fields__
    )
    check = f"    s._ensure_dumpable_orm_state({checked!r})\n" if checked else ""
    source = f"def dump(s):\n{check}    return {{{', '.join(items)}}}\n"
    exec(compile(source, f"<dump of {cls.__qualname__}>", "exec"), namespace)  # noqa: S102 - see the docstring
    return namespace["dump"]


def _field_type_may_hold_orm_state(field_type: Any) -> bool:
    """Return True when a field type could plausibly hold a manager/queryset at runtime."""
    if field_type in {
        Any,
        object,
        BaseManager,
        QuerySet,
        DjangoModel,
        list,
        tuple,
        set,
        frozenset,
        dict,
        Collection,
        Iterable,
        Mapping,
        Sequence,
    }:
        return True

    origin = get_origin(field_type)
    if origin is None:
        return False

    if origin is Literal:
        return False

    if origin in {Union, UnionType}:
        return any(_field_type_may_hold_orm_state(arg) for arg in get_args(field_type) if arg is not type(None))

    if origin is Annotated:
        args = get_args(field_type)
        return bool(args) and _field_type_may_hold_orm_state(args[0])

    if origin in {list, tuple, set, frozenset, dict, Collection, Iterable, Mapping, Sequence}:
        args = tuple(arg for arg in get_args(field_type) if arg not in {type(None), Ellipsis})
        return not args or any(_field_type_may_hold_orm_state(arg) for arg in args)

    return False


def _get_django_relation_info(model_cls: type, attr_name: str) -> _DjangoRelationInfo | None:
    """Return cached Django relation metadata for a model accessor.

    Uses weak model-class keys so dev reloads don't pin stale model classes in memory.
    """
    model_cache = _DJANGO_RELATION_CACHE.get(model_cls)
    if model_cache is None:
        model_cache = {}
        _DJANGO_RELATION_CACHE[model_cls] = model_cache
    elif attr_name in model_cache:
        return model_cache[attr_name]

    meta = getattr(model_cls, "_meta", None)
    if meta is None:
        model_cache[attr_name] = None
        return None

    try:
        field = meta.get_field(attr_name)
    except FieldDoesNotExist:
        model_cache[attr_name] = None
        return None

    if not getattr(field, "is_relation", False):
        model_cache[attr_name] = None
        return None

    # Read `cache_name`. Django 5.2 relations raise NotImplementedError from the
    # deprecated get_cache_name(), and Django 6.0 removes it.
    cache_name = getattr(field, "cache_name", attr_name)

    related_model = getattr(field, "related_model", None)

    if getattr(field, "auto_created", False) and not getattr(field, "concrete", True):
        if getattr(field, "one_to_one", False):
            reverse_field = getattr(field, "field", None)
            reverse_query_attname = getattr(reverse_field, "attname", None) or getattr(reverse_field, "name", None)
            info = _DjangoRelationInfo(
                name=attr_name,
                kind="reverse_one",
                cache_name=cache_name,
                related_model=related_model,
                reverse_query_attname=reverse_query_attname,
            )
            model_cache[attr_name] = info
            return info

        info = _DjangoRelationInfo(
            name=attr_name,
            kind="many",
            cache_name=attr_name,
            related_model=related_model,
        )
        model_cache[attr_name] = info
        return info

    if getattr(field, "many_to_many", False):
        info = _DjangoRelationInfo(
            name=attr_name,
            kind="many",
            cache_name=attr_name,
            related_model=related_model,
        )
        model_cache[attr_name] = info
        return info

    if getattr(field, "one_to_one", False) or getattr(field, "many_to_one", False):
        info = _DjangoRelationInfo(
            name=attr_name,
            kind="forward_one",
            cache_name=cache_name,
            related_model=related_model,
            attname=getattr(field, "attname", None),
        )
        model_cache[attr_name] = info
        return info

    model_cache[attr_name] = None
    return None


# msgspec makes a new object from an empty default of these types for each instance.
_EMPTY_MUTABLE_DEFAULTS = (list, dict, set, bytearray)


def _read_only_marker(default: Any) -> Any:
    """
    The ``field()`` marker for a ``Config.read_only`` field with this plain default.

    Return ``_MISSING`` when the default needs no marker: a marker, a msgspec
    field, a method or property, or a non-empty mutable default.
    """
    if isinstance(default, (_FieldMarker, _MSGSPEC_FIELD)) or hasattr(default, "__get__"):
        return _MISSING
    if type(default) is _MSGSPEC_FACTORY:
        return field(read_only=True, default_factory=default.factory)
    if isinstance(default, _EMPTY_MUTABLE_DEFAULTS):
        if default:
            # msgspec refuses this default with its own message.
            return _MISSING
        return field(read_only=True, default_factory=type(default))
    return field(read_only=True, default=default)


def _own_annotations(cls: type) -> Mapping[str, Any]:
    """The annotations of the class body of ``cls``. Names that do not resolve stay as they are."""
    if sys.version_info >= (3, 14):
        return annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF)
    return cls.__dict__.get("__annotations__", {})


def _namespace_annotation_names(namespace: dict[str, Any]) -> Collection[str]:
    """The annotated names of a class body that is not a class yet."""
    if "__annotations__" in namespace:
        return namespace["__annotations__"]
    annotate = namespace.get("__annotate_func__")
    if annotate is None:
        return ()
    return annotationlib.call_annotate_function(annotate, annotationlib.Format.FORWARDREF)


def _resolve_base_annotation(class_name: str, owner: type, field_name: str) -> Any:
    """
    Resolve the annotation of ``field_name`` in the class body of ``owner``.

    A string annotation resolves in the module of ``owner``, as msgspec does it.
    A name that does not resolve now, or a type parameter of ``owner``, raises
    ``ImproperlyConfigured``.
    """
    annotation: Any
    if sys.version_info >= (3, 14):
        try:
            annotation = annotationlib.get_annotations(owner)[field_name]
        except NameError:
            # Another annotation of ``owner`` does not resolve. Read the text of this one.
            annotation = annotationlib.get_annotations(owner, format=annotationlib.Format.STRING)[field_name]
    else:
        annotation = owner.__dict__["__annotations__"][field_name]

    def fail(problem: str) -> ImproperlyConfigured:
        return ImproperlyConfigured(
            f"{class_name}.Config.read_only names `{field_name}`, a field of {owner.__qualname__}. "
            f"{problem} Declare the field in {class_name} with "
            f"`{field_name}: <type> = field(read_only=True, default=...)`."
        )

    if isinstance(annotation, str):
        # msgspec and typing.get_type_hints look up a name in the module first,
        # then in the class body.
        class_locals = {param.__name__: param for param in getattr(owner, "__type_params__", ())}
        class_locals.update(vars(owner))
        module_globals = getattr(sys.modules.get(owner.__module__), "__dict__", {})
        try:
            annotation = eval(annotation, class_locals, module_globals)  # noqa: S307
        except NameError as exc:
            raise fail(f"Its annotation {annotation!r} does not resolve at class creation: {exc}.") from exc

    type_params = getattr(owner, "__parameters__", ())
    if type_params and (
        any(annotation is param for param in type_params)
        or any(param in type_params for param in getattr(annotation, "__parameters__", ()))
    ):
        raise fail(f"Its annotation {annotation!r} uses a type parameter of {owner.__qualname__}.")
    return annotation


def _add_namespace_annotations(namespace: dict[str, Any], added: dict[str, Any]) -> None:
    """Add resolved annotations to a class body, before msgspec reads it."""
    if "__annotations__" in namespace or "__annotate_func__" not in namespace:
        namespace["__annotations__"] = {**namespace.get("__annotations__", {}), **added}
        return

    # Python 3.14 (PEP 649): the class body has an annotate function.
    original = namespace["__annotate_func__"]

    def annotate(format: int) -> dict[str, Any]:  # noqa: A002 (the PEP 649 name)
        if format == annotationlib.Format.VALUE_WITH_FAKE_GLOBALS:
            raise NotImplementedError
        annotations = annotationlib.call_annotate_function(original, format)
        if format == annotationlib.Format.STRING:
            return {**annotations, **{name: annotationlib.type_repr(value) for name, value in added.items()}}
        return {**annotations, **added}

    namespace["__annotate_func__"] = annotate


def _mark_config_read_only_defaults(name: str, bases: tuple, namespace: dict[str, Any]) -> None:
    """
    Give each ``Config.read_only`` field with a plain default a ``field()`` marker default.

    ``id: int = 0`` with ``Config.read_only = {"id"}`` then is the same as
    ``id: int = field(read_only=True, default=0)``. The marker tells an absent
    field from an input value that is equal to the default.

    A field of a base class gets a declaration in the class body, with the
    resolved annotation of the base class and the marker. A ``msgspec.field()``
    default of the class body keeps its struct default.
    """
    config = namespace.get("Config")
    read_only = getattr(config, "read_only", None)
    if not read_only:
        return
    added_annotations: dict[str, Any] = {}
    own_names: Collection[str] | None = None
    for field_name in read_only:
        if field_name in namespace:
            marker = _read_only_marker(namespace[field_name])
            if marker is not _MISSING:
                namespace[field_name] = marker
            continue
        if own_names is None:
            own_names = _namespace_annotation_names(namespace)
        if field_name in own_names:
            # The class body declares the field with no default.
            continue
        base = next((base for base in bases if field_name in getattr(base, "__struct_fields__", ())), None)
        if base is None:
            continue
        default = dict(_iter_field_defaults(base)).get(field_name, msgspec.NODEFAULT)
        # A field with no default keeps the error of a read_only field with no default.
        marker = _MISSING if default is msgspec.NODEFAULT else _read_only_marker(default)
        if marker is _MISSING:
            continue
        owner = next(cls for cls in base.__mro__ if field_name in _own_annotations(cls))
        added_annotations[field_name] = _resolve_base_annotation(name, owner, field_name)
        encode_name = dict(zip(base.__struct_fields__, base.__struct_encode_fields__, strict=True))[field_name]
        # The field keeps the JSON name of the base class.
        namespace[field_name] = marker if encode_name == field_name else msgspec.field(default=marker, name=encode_name)
    if added_annotations:
        _add_namespace_annotations(namespace, added_annotations)


class _SerializerMeta(_STRUCT_META):
    """
    Custom metaclass that forces kw_only=True for all Serializer subclasses.

    This allows mixing required and optional fields in any order (like Pydantic/DRF),
    without requiring users to add kw_only=True to every serializer class.
    """

    def __new__(mcs, name: str, bases: tuple, namespace: dict, **kwargs):
        # Force kw_only=True for all Serializer subclasses
        kwargs.setdefault("kw_only", True)
        _mark_config_read_only_defaults(name, bases, namespace)

        # field() with no default is not a default value. Take the marker out of
        # the class body, so msgspec reports a missing value as it does for a
        # plain required field. The config stays on the class.
        required_field_configs = {
            field_name: value.config
            for field_name, value in namespace.items()
            if isinstance(value, _FieldMarker) and not value.config.has_default()
        }
        # field(alias=...) is the name of the field in JSON, for input and for
        # output. msgspec has one such name for each field, so the alias goes
        # there. A required field keeps only the name. A field with a default
        # keeps the marker as the default, as for a field with no alias.
        for field_name, config in required_field_configs.items():
            if config.alias:
                namespace[field_name] = msgspec.field(name=config.alias)
            else:
                del namespace[field_name]
        for field_name, value in list(namespace.items()):
            if isinstance(value, _FieldMarker) and value.config.alias:
                namespace[field_name] = msgspec.field(default=value, name=value.config.alias)
        cls = cast(Any, super().__new__(mcs, name, bases, namespace, **kwargs))

        # A subclass keeps the config of its parents. A field that the subclass
        # declares again with a default uses that default, not the old config.
        defaults = dict(_iter_field_defaults(cls))
        inherited = getattr(cls, "__required_field_configs__", {})
        cls.__required_field_configs__ = {
            **{field_name: config for field_name, config in inherited.items() if field_name not in defaults},
            **required_field_configs,
        }

        # Build rename map (e.g., {"user_name": "userName"} for rename="camel").
        # Done here because __struct_fields__ isn't available in __init_subclass__.
        # If forward references can't be resolved, deferred to _lazy_collect_field_configs.
        rename_map = _build_rename_map(cast(type[msgspec.Struct], cls)) if hasattr(cls, "__struct_fields__") else {}
        cls.__rename_map__ = rename_map
        # Fast boolean flag avoids dict truthiness check on every dump()
        cls.__has_rename__ = bool(rename_map)

        # Capture tag configuration from msgspec. msgspec resolves tag=True
        # and tag=<callable> to the final tag string at class creation time,
        # so __struct_config__.tag is always None or the resolved value.
        struct_config = getattr(cls, "__struct_config__", None)
        cls.__tag_value__ = getattr(struct_config, "tag", None)
        cls.__tag_field__ = getattr(struct_config, "tag_field", None) or "type"

        return cls


class Serializer(msgspec.Struct, metaclass=_SerializerMeta):
    """
    Enhanced msgspec.Struct with validation and Django model integration.

    Features:
    - Field validation via @field_validator decorator
    - Model-level validation via @model_validator decorator
    - Django model integration (from_model, to_dict, to_model)
    - Full type safety for IDE/type checkers
    - All msgspec.Struct features (frozen, array_like, etc.)
    - kw_only=True by default: Mix required and optional fields in any order (like Pydantic/DRF)

    Example:
        class UserCreate(Serializer):
            id: int = field(read_only=True, default=0)  # Optional field can come first
            username: str                     # Required field can come after - OK!
            email: str
            password: str

            @field_validator('email')
            def validate_email(cls, value):
                if '@' not in value:
                    raise ValueError('Invalid email')
                return value.lower()

            @model_validator
            def validate_username_unique(self):
                from django.contrib.auth.models import User
                if User.objects.filter(username=self.username).exists():
                    raise ValueError('Username already exists')

        # Must use keyword arguments for instantiation:
        user = UserCreate(username="john", email="john@example.com", password="secret")
    """

    # Unique marker to identify Serializer instances (for type checking in _convert_serializers)
    __is_bolt_serializer__: ClassVar[bool] = True

    # Class attributes for validators (populated by __init_subclass__)
    __field_validators__: ClassVar[dict[str, list[Any]]] = {}
    __model_validators__: ClassVar[list[Any]] = []
    # Pre-computed tuple of (field_name, validators_tuple) for faster iteration.
    # It has the validators with mode="after". __field_validators__ has each mode, in definition order.
    __field_validators_tuple__: ClassVar[tuple[tuple[str, tuple[Any, ...]], ...]] = ()
    # Validators with mode="before", in the order that they run.
    __before_field_validators__: ClassVar[tuple[tuple[str, tuple[Any, ...]], ...]] = ()
    __before_model_validators__: ClassVar[tuple[Any, ...]] = ()
    # How model_validate() runs the before validators of this class and of its
    # nested serializers: PLAN_NOT_BUILT until the first input, then the plan,
    # or None for a class that keeps the direct msgspec decode.
    __before_plan__: ClassVar[Any] = None
    _computed_dump_specs: ClassVar[tuple[_ComputedDumpSpec, ...]] = ()

    # Cached type hints and metadata (populated by __init_subclass__)
    __cached_type_hints__: ClassVar[dict[str, Any]] = {}
    __nested_fields__: ClassVar[dict[str, Any]] = {}
    __literal_fields__: ClassVar[dict[str, frozenset[Any]]] = {}  # Frozenset for O(1) lookup
    __model_field_specs__: ClassVar[tuple[_ModelFieldSpec, ...]] = ()

    # Field configuration (populated by __init_subclass__)
    __field_configs__: ClassVar[dict[str, FieldConfig]] = {}
    __computed_fields__: ClassVar[dict[str, ComputedFieldConfig]] = {}
    __read_only_fields__: ClassVar[frozenset[str]] = frozenset()
    # (field name, name in the input, default, default factory, is field() marker)
    # of each read_only field. See _read_only_input_default().
    __read_only_input_fields__: ClassVar[tuple[tuple[str, str, Any, Any, bool], ...]] = ()
    # True if the struct forbids unknown fields: an input value for a read_only field is an error.
    __read_only_input_forbidden__: ClassVar[bool] = False
    # Tells if input of this type can set a read_only field, here or in a
    # nested serializer. None = not computed yet.
    __read_only_input__: ClassVar[bool | None] = None
    __write_only_fields__: ClassVar[frozenset[str]] = frozenset()
    __source_mapping__: ClassVar[dict[str, str]] = {}  # API field name -> source attribute
    __field_sets__: ClassVar[dict[str, list[str]]] = {}  # Named field sets for use() method

    # Config of each field that has field() with no default (set by the metaclass)
    __required_field_configs__: ClassVar[dict[str, FieldConfig]] = {}

    # Fields defined with field() (populated by __init_subclass__)
    __field_marker_fields__: ClassVar[tuple[str, ...]] = ()

    # Fast-path flags: Control which validation runs (set at class definition time)
    __skip_validation__: ClassVar[bool] = True  # Skip all validation
    __has_nested_or_literal__: ClassVar[bool] = False  # Has nested/literal fields
    __has_field_validators__: ClassVar[bool] = False  # Has custom field validators
    __has_model_validators__: ClassVar[bool] = False  # Has model validators
    __has_computed_fields__: ClassVar[bool] = False  # Has computed fields
    __has_field_markers__: ClassVar[bool] = False  # Has fields defined with field(), or read_only fields
    __field_configs_collected__: ClassVar[bool] = False  # Lazy config collection done

    # Fast-path flag for dump: True if dump can use simple/fast path
    __dump_fast_path__: ClassVar[bool] = True
    # The dump() of a class with no fast path and no options, made when the class is ready to dump.
    __dump_function__: ClassVar[Callable[[Any], dict[str, Any]] | None] = None
    __orm_state_check_fields__: ClassVar[tuple[str, ...]] = ()
    _dump_field_specs: ClassVar[tuple[_DumpFieldSpec, ...]] = ()
    _dump_field_spec_cache: ClassVar[
        dict[tuple[frozenset[str] | None, frozenset[str] | None], tuple[_DumpFieldSpec, ...]]
    ] = {}
    _computed_dump_spec_cache: ClassVar[
        dict[tuple[frozenset[str] | None, frozenset[str] | None], tuple[_ComputedDumpSpec, ...]]
    ] = {}
    _serializer_view_cache: ClassVar[dict[tuple[frozenset[str] | None, frozenset[str] | None], Any]] = {}
    # Track if any field is a Serializer type (requires recursive dump)
    __has_serializer_fields__: ClassVar[bool] = False
    # Rename mapping: Python field name -> encoded name (e.g., "user_name" -> "userName")
    # Populated when msgspec rename= is used (e.g., rename="camel")
    __rename_map__: ClassVar[dict[str, str]] = {}
    # Fast boolean flag for rename check (avoids dict truthiness check on every dump)
    __has_rename__: ClassVar[bool] = False

    # Tag tracking properties
    __tag_value__: ClassVar[Any | None] = None
    __tag_field__: ClassVar[str] = "type"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Collect validators and cache type hints when a subclass is created."""

        # msgspec.Struct kwargs (kw_only, tag, rename, etc.) are consumed by
        # _SerializerMeta.__new__ and validated there, but Python still forwards
        # them to __init_subclass__. object.__init_subclass__ rejects any kwargs,
        # so swallow them here instead of maintaining a hand-rolled allowlist
        # that would silently break each time msgspec adds a new struct option.
        super().__init_subclass__()
        # Collect validators for this class
        cls.__field_validators__ = collect_field_validators(cls)
        cls.__model_validators__ = collect_model_validators(cls)
        cls.__computed_fields__ = collect_computed_fields(cls)
        cls._computed_dump_specs = tuple(
            _ComputedDumpSpec(field_name=field_name, method_name=config.method_name)
            for field_name, config in cls.__computed_fields__.items()
        )

        # Pre-compute validators as tuple for faster iteration (no dict overhead)
        cls.__field_validators_tuple__ = tuple(
            (field_name, tuple(validators))
            for field_name, validators in collect_field_validators(cls, mode="after").items()
        )
        cls.__before_field_validators__ = tuple(
            (field_name, tuple(validators))
            for field_name, validators in collect_field_validators(cls, mode="before").items()
        )
        cls.__before_model_validators__ = tuple(collect_model_validators(cls, mode="before"))
        cls.__before_plan__ = PLAN_NOT_BUILT
        # Only a class with a before validator gets the slower validation step.
        # Each other class keeps _run_field_validators() as it is.
        if cls.__before_field_validators__ or cls.__before_model_validators__:
            cls._run_field_validators = Serializer._run_before_then_field_validators

        # A subclass must rebuild its own specs even when the parent already
        # collected (otherwise it inherits ``True`` and dumps ``{}``).
        cls.__field_configs_collected__ = False
        cls.__read_only_input__ = None
        cls._dump_field_specs = ()
        cls._dump_field_spec_cache = {}
        cls._computed_dump_spec_cache = {}
        cls._serializer_view_cache = {}
        cls.__orm_state_check_fields__ = ()

        # Collect configuration from Meta class (read_only, write_only, field_sets)
        # Note: _FieldMarker processing is deferred to _lazy_collect_field_configs
        cls._collect_meta_config()

        # Cache type hints and field metadata (expensive - do once!)
        cls._cache_type_metadata()

        # Set fast-path flags to control which validation runs
        cls.__has_nested_or_literal__ = bool(cls.__nested_fields__ or cls.__literal_fields__)
        cls.__has_field_validators__ = bool(cls.__field_validators__)
        cls.__has_model_validators__ = bool(cls.__model_validators__ or cls.__before_model_validators__)
        cls.__has_computed_fields__ = bool(cls.__computed_fields__)

        # Skip all validation if there's nothing to validate
        cls.__skip_validation__ = not (
            cls.__has_nested_or_literal__ or cls.__has_field_validators__ or cls.__has_model_validators__
        )

        # Note: default values map is cached lazily on first dump(exclude_defaults=True)
        # because __struct_defaults__ may not be set yet in __init_subclass__

        # Determine if dump can use fast path (no special handling needed)
        # Fast path requires:
        # - No computed fields
        # - No write_only fields
        # - No field configs with exclude
        # - No nested serializer fields
        # - No serializer-typed fields (they need recursive dump)
        cls.__dump_fast_path__ = (
            not cls.__has_computed_fields__
            and not cls.__write_only_fields__
            and not cls.__nested_fields__
            and not cls.__has_serializer_fields__
            and not any(cfg.exclude for cfg in cls.__field_configs__.values())
        )

    @classmethod
    def _collect_meta_config(cls) -> None:
        """
        Collect configuration from Meta class (read_only, write_only, field_sets).

        This is called in __init_subclass__ to handle Meta class attributes.
        The _FieldMarker processing is done lazily in _lazy_collect_field_configs
        because __struct_defaults__ isn't populated yet at class definition time.
        """
        read_only: set[str] = set()
        write_only: set[str] = set()

        # Check Config class for read_only/write_only sets (renamed from Meta to avoid conflict with msgspec.Meta)
        meta = getattr(cls, "Config", None)
        if meta:
            # Nothing ran Config.validators. Reject it: a class that sets it looks validated and is not.
            if hasattr(meta, "validators"):
                raise TypeError(
                    f"{cls.__name__}.Config.validators is not an option. "
                    f"Declare each validator with @field_validator or @model_validator."
                )
            meta_read_only = getattr(meta, "read_only", set())
            meta_write_only = getattr(meta, "write_only", set())
            meta_field_sets = getattr(meta, "field_sets", {})

            read_only.update(meta_read_only)
            write_only.update(meta_write_only)

            # Store field_sets on class for use() method
            cls.__field_sets__ = meta_field_sets

        # Initialize with Meta values (will be updated by _lazy_collect_field_configs)
        cls.__field_configs__ = {}
        cls.__field_marker_fields__ = ()
        cls.__read_only_fields__ = frozenset(read_only)
        cls.__read_only_input_fields__ = ()
        cls.__write_only_fields__ = frozenset(write_only)
        cls.__source_mapping__ = {}
        cls.__has_field_markers__ = False

    @classmethod
    def _cache_type_metadata(cls) -> None:
        """
        Cache type hints and field metadata at class definition time.

        This is called ONCE per class (in __init_subclass__), not per instance.
        This is critical for performance - moving from 10K ops/sec to 1.75M ops/sec!

        Note: Function-scoped serializer classes (classes defined inside functions) have
        limited support and may not resolve all type hints correctly, especially when
        using forward references or complex generic types. This is due to Python's
        type hint resolution requiring access to the local namespace where the class
        was defined. For best results and full type hint resolution, always define
        serializers at module level.
        """
        # Track all frame references for proper cleanup to prevent memory leaks
        frames_to_cleanup = []
        try:
            # Strategy 1: Try with local namespace for function-scoped classes
            # This handles edge cases but adds complexity
            frame = inspect.currentframe()
            if frame is not None:
                frames_to_cleanup.append(frame)

            localns = {}

            # Walk up to 10 frames to find class definition context
            for _ in range(10):
                if frame is None:
                    break
                localns.update(frame.f_locals)
                frame = frame.f_back
                if frame is not None:
                    frames_to_cleanup.append(frame)

            # Resolve type hints with local namespace
            hints = get_type_hints(cls, globalns=None, localns=localns, include_extras=True)

        except Exception:
            # Strategy 2: Fallback without local namespace (module-level classes)
            try:
                hints = get_type_hints(cls)
            except Exception:
                # Last resort: use raw annotations
                hints = getattr(cls, "__annotations__", {})
        finally:
            # Clean up all frame references to prevent memory leaks
            for f in frames_to_cleanup:
                del f
            frames_to_cleanup.clear()

        # Cache the type hints
        cls.__cached_type_hints__ = hints

        # Pre-compute nested field configurations
        nested_fields = {}
        literal_fields = {}
        has_serializer_fields = False  # Track if any field is a Serializer (for dump optimization)

        for field_name, field_type in hints.items():
            # Infer nested serializer behavior from the field type.
            nested_config = resolve_nested_config(field_type)
            if nested_config is not None:
                nested_fields[field_name] = nested_config
                has_serializer_fields = True

            # Check if field is a Literal type (for Django choices validation)
            origin = get_origin(field_type)
            if origin is Literal:
                allowed_values = get_args(field_type)
                # Convert to frozenset for O(1) membership testing (optimization #3)
                literal_fields[field_name] = frozenset(allowed_values)

        cls.__nested_fields__ = nested_fields
        cls.__literal_fields__ = literal_fields
        cls.__has_serializer_fields__ = has_serializer_fields

    def __post_init__(self) -> None:
        """
        Run all field and model validators after struct initialization.

        Also fixes _FieldMarker defaults - msgspec stores the _FieldMarker object
        as the default value, so we need to replace it with the actual default.

        Validators are executed in order:
        1. Fix _FieldMarker defaults (only if field() is used)
        2. Model validators with mode='before'
        3. Field validators with mode='before'
        4. Field validators with mode='after'
        5. Model validators with mode='after'

        Steps 2 and 3 run here only for an instance that Python code builds.
        For input (a request body, model_validate(), model_validate_json()),
        they run on the raw data before msgspec builds the instance.
        """
        cls = self.__class__

        # Lazy field config collection - run once per class when first instance is created
        # This is needed because __init_subclass__ runs BEFORE msgspec sets __struct_defaults__
        if not cls.__field_configs_collected__:
            cls._lazy_collect_field_configs()

        # OPTIMIZATION: Only check for _FieldMarker defaults if the class uses field()
        # This avoids iterating through all fields for simple serializers
        if cls.__has_field_markers__:
            # Input cannot set a read_only field. A class with no field() marker
            # and no read_only field does not come here.
            if cls.__read_only_input_fields__ and INPUT_MODE.get():
                self._reset_read_only_fields()
            self._fix_field_marker_defaults_impl()

        # Fast path: skip validation if there are no validators
        # This avoids function call overhead when there's nothing to validate
        if cls.__skip_validation__:
            return

        # Collect all validation errors instead of stopping at first
        errors: list[dict] = []

        # Run field validators (includes nested/literal validation)
        errors.extend(self._run_field_validators())

        # Run model validators
        errors.extend(self._run_model_validators())

        # Raise all errors at once if any occurred
        if errors:
            raise RequestValidationError(errors=errors)

    def _reset_read_only_fields(self) -> None:
        """
        Give each ``read_only`` field its default, in place of the input value.

        A struct that forbids unknown fields rejects the input value, as it
        rejects an unknown field. msgspec adds the path of a nested struct to
        the message of the ``ValueError``.
        """
        cls = self.__class__
        forbid_unknown = cls.__read_only_input_forbidden__
        _setattr = msgspec_structs.force_setattr

        for field_name, input_name, default, factory, is_marker in cls.__read_only_input_fields__:
            if factory is not None:
                default = factory()
            elif default is msgspec.NODEFAULT:
                raise ImproperlyConfigured(cls._read_only_no_default_message(field_name))
            if forbid_unknown:
                value = getattr(self, field_name)
                # An absent ``field()`` field holds its marker. Config.read_only
                # gives a plain default a marker too, also for a field of a base
                # class. A ``msgspec.field()`` default of the class body has no
                # such sign, so an input value equal to it passes.
                if not isinstance(value, _FieldMarker) if is_marker else value != default:
                    raise ValueError(f"Object contains unknown field `{input_name}`")
            _setattr(self, field_name, default)

    @classmethod
    def _read_only_no_default_message(cls, field_name: str) -> str:
        return (
            f"{cls.__name__}.{field_name} is read_only and has no default. Input cannot set a "
            f"read_only field. Give the field a default (for example `field(read_only=True, default=None)`), "
            f"or use a serializer without the field for input."
        )

    @classmethod
    def _check_read_only_input(cls) -> bool:
        """Tell if this class has a ``read_only`` field. Each such field must have a default."""
        if not cls.__field_configs_collected__:
            cls._lazy_collect_field_configs()
        for field_name, _input_name, default, factory, _is_marker in cls.__read_only_input_fields__:
            if factory is None and default is msgspec.NODEFAULT:
                raise ImproperlyConfigured(cls._read_only_no_default_message(field_name))
        return bool(cls.__read_only_input_fields__)

    def _fix_field_marker_defaults_impl(self) -> None:
        """
        Replace _FieldMarker instances with their actual default values.

        When using field(), msgspec stores the _FieldMarker object as the default.
        This method checks if any field values are _FieldMarker instances and
        replaces them with the configured default value.

        This method is only called when __has_field_markers__ is True (set at class
        creation time), avoiding the loop for simple serializers without field().
        """
        _setattr = msgspec_structs.force_setattr

        for field_name in self.__class__.__field_marker_fields__:
            current_value = getattr(self, field_name)

            # Check if the current value is a _FieldMarker (meaning the user
            # didn't provide a value and msgspec used the marker as default)
            if isinstance(current_value, _FieldMarker):
                # Replace with actual default from the _FieldMarker config. A field()
                # with no default is a required field of the struct, not a marker.
                _setattr(self, field_name, current_value.config.get_default())

    @classmethod
    def _lazy_collect_field_configs(cls) -> None:
        """
        Lazily collect field configs from _FieldMarker defaults.

        This runs once per class on first instance creation, because at this point
        msgspec has already processed the class and set __struct_defaults__.
        """
        field_configs: dict[str, FieldConfig] = {}
        field_marker_fields: list[str] = []
        read_only: set[str] = set()
        write_only: set[str] = set()
        source_mapping: dict[str, str] = {}

        # Use helper to iterate over (field_name, default_value) pairs
        for field_name, default_val in _iter_field_defaults(cls):
            if isinstance(default_val, _FieldMarker):
                config = default_val.config
                field_configs[field_name] = config
                field_marker_fields.append(field_name)

                if config.read_only:
                    read_only.add(field_name)
                if config.write_only:
                    write_only.add(field_name)
                if config.source:
                    source_mapping[field_name] = config.source

        for field_name, config in cls.__required_field_configs__.items():
            field_configs[field_name] = config
            if config.read_only:
                read_only.add(field_name)
            if config.write_only:
                write_only.add(field_name)
            if config.source:
                source_mapping[field_name] = config.source

        has_field_markers = bool(field_marker_fields)

        # Merge with existing configs from Meta class
        cls.__field_configs__.update(field_configs)
        cls.__read_only_fields__ = cls.__read_only_fields__ | frozenset(read_only)
        struct_defaults = dict(_iter_field_defaults(cls))
        cls.__read_only_input_fields__ = tuple(
            (
                field_name,
                cls.__rename_map__.get(field_name, field_name),
                *_read_only_input_default(struct_defaults.get(field_name, msgspec.NODEFAULT)),
            )
            for field_name in cls.__struct_fields__
            if field_name in cls.__read_only_fields__
        )
        cls.__read_only_input_forbidden__ = cls.__struct_config__.forbid_unknown_fields
        cls.__write_only_fields__ = cls.__write_only_fields__ | frozenset(write_only)
        cls.__source_mapping__.update(source_mapping)
        cls.__field_marker_fields__ = tuple(field_marker_fields)

        default_field_names = {field_name for field_name, _ in _iter_field_defaults(cls)}
        cls.__model_field_specs__ = tuple(
            _ModelFieldSpec(
                field_name=field_name,
                source=cls.__source_mapping__.get(field_name),
                nested=_get_model_output_nested(cls.__cached_type_hints__.get(field_name, Any)),
                has_default=field_name in default_field_names,
            )
            for field_name in cls.__struct_fields__
        )

        # Update the has_field_markers flag based on actual _FieldMarker defaults found
        # Config.read_only needs the same step of __post_init__ as a marker.
        cls.__has_field_markers__ = has_field_markers or bool(cls.__read_only_input_fields__)

        # Build rename map if deferred from _SerializerMeta.__new__ (forward reference)
        if not cls.__has_rename__ and not cls.__rename_map__:
            rename_map = _build_rename_map(cls)
            cls.__rename_map__ = rename_map
            cls.__has_rename__ = bool(rename_map)

        cls._dump_field_specs = cls._build_dump_field_specs()
        # Recalculate __dump_fast_path__ now that we have the actual field configs
        # This is needed because write_only fields from _FieldMarker weren't available
        # in __init_subclass__ when __dump_fast_path__ was first computed
        cls.__dump_fast_path__ = (
            not cls.__has_computed_fields__
            and not cls.__write_only_fields__
            and not cls.__nested_fields__
            and not cls.__has_serializer_fields__
            and not any(cfg.exclude for cfg in cls.__field_configs__.values())
            and not any(spec.dumper is not None for spec in cls._dump_field_specs)
        )
        cls.__orm_state_check_fields__ = tuple(
            field_name
            for field_name in cls.__struct_fields__
            if _field_type_may_hold_orm_state(cls.__cached_type_hints__.get(field_name, Any))
        )
        cls.__dump_function__ = None if cls.__dump_fast_path__ else _compile_dump_function(cls)
        cls._dump_field_spec_cache.clear()
        cls._computed_dump_spec_cache.clear()
        cls._serializer_view_cache.clear()

        # Mark as collected so we don't run again
        cls.__field_configs_collected__ = True

    def _run_field_validators(self) -> list[dict]:
        """Execute all field validators, collecting all errors instead of stopping at first.

        Returns:
            List of error dicts with 'loc', 'msg', and 'type' keys.
        """
        errors: list[dict] = []

        # First, validate nested/literal fields if any exist
        if self.__has_nested_or_literal__:
            errors.extend(self._validate_nested_and_literal_fields())

        # Then run custom field validators if any exist
        if self.__has_field_validators__:
            # Cache lookups at method level (class reference is from __post_init__)
            _class = self.__class__
            _setattr = msgspec_structs.force_setattr
            _getattr = getattr

            # Use pre-computed tuple for faster iteration (no dict.items() overhead)
            for field_name, validators in _class.__field_validators_tuple__:
                try:
                    # Get current value once
                    current_value = _getattr(self, field_name)

                    # Run all validators for this field
                    # Note: validators MUST return the value (not None for pass-through)
                    for validator in validators:
                        result = validator(_class, current_value)
                        # If validator returns None, keep the original value
                        # This allows validators to validate without transforming
                        if result is not None:
                            current_value = result

                    # Update the field once with the final value
                    _setattr(self, field_name, current_value)

                except (ValueError, TypeError) as e:
                    # Collect error instead of raising immediately. The loc has the
                    # wire name of the field, as a msgspec error has.
                    errors.append(
                        {
                            "loc": ["body", _class.__rename_map__.get(field_name, field_name)],
                            "msg": str(e),
                            "type": "value_error",
                        }
                    )

        return errors

    def _run_before_then_field_validators(self) -> list[dict]:
        """_run_field_validators() of a class that has a validator with mode="before"."""
        if not RAW_VALIDATED.get():
            # Python code built this instance. No raw input went through the plan.
            errors = self._run_before_validators()
            if errors:
                return errors
        return Serializer._run_field_validators(self)

    def _run_before_validators(self) -> list[dict]:
        """
        Run the before validators on an instance that Python code built.

        msgspec does no type conversion for such an instance, so the validators
        get the values of the fields. A model validator gets them as a dict with
        the input names of the fields, and Bolt sets the fields from its result.
        """
        cls = self.__class__
        errors: list[dict] = []
        _setattr = msgspec_structs.force_setattr

        if cls.__before_model_validators__:
            rename_map = cls.__rename_map__
            input_names = [(name, rename_map.get(name, name)) for name in cls.__struct_fields__]
            data: Any = {input_name: getattr(self, name) for name, input_name in input_names}
            try:
                for validator in cls.__before_model_validators__:
                    data = validator(cls, data)
            except (ValueError, TypeError) as e:
                return [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            if isinstance(data, dict):
                for name, input_name in input_names:
                    if input_name in data:
                        _setattr(self, name, data[input_name])

        for field_name, validators in cls.__before_field_validators__:
            try:
                value = getattr(self, field_name)
                for validator in validators:
                    value = validator(cls, value)
                _setattr(self, field_name, value)
            except (ValueError, TypeError) as e:
                errors.append({"loc": ["body", field_name], "msg": str(e), "type": "value_error"})

        return errors

    def _validate_nested_and_literal_fields(self) -> list[dict]:
        """
        Validate nested serializer fields and Literal (choice) fields using cached metadata.

        This method handles two types of validation:
        1. Nested fields: Fields whose type resolves to a nested serializer
        2. Literal fields: Fields with Literal[] type hints that restrict values to specific choices

        Returns:
            List of error dicts with 'loc', 'msg', and 'type' keys.
        """
        errors: list[dict] = []

        # Cache force_setattr for nested field validation (optimization #2)
        _setattr = msgspec_structs.force_setattr
        # The loc of an error has the wire name of the field, as a msgspec error has.
        rename_map = self.__class__.__rename_map__

        # Validate nested fields (no hasattr needed - msgspec struct fields always exist)
        for field_name, nested_config in self.__nested_fields__.items():
            try:
                current_value = getattr(self, field_name)
                validated_value = validate_nested_field(current_value, nested_config, field_name)

                # Update the field if validation changed it
                if validated_value is not current_value:
                    _setattr(self, field_name, validated_value)
            except (ValueError, TypeError) as e:
                errors.append(
                    {
                        "loc": ["body", rename_map.get(field_name, field_name)],
                        "msg": str(e),
                        "type": "value_error",
                    }
                )

        # Validate literal (choice) fields (now with O(1) frozenset lookup - optimization #3)
        for field_name, allowed_values in self.__literal_fields__.items():
            current_value = getattr(self, field_name)
            if current_value not in allowed_values:
                errors.append(
                    {
                        "loc": ["body", rename_map.get(field_name, field_name)],
                        "msg": f"invalid value {current_value!r}. Expected one of: {', '.join(repr(v) for v in allowed_values)}",
                        "type": "value_error",
                    }
                )

        return errors

    def _run_model_validators(self) -> list[dict]:
        """Execute all model validators, collecting all errors instead of stopping at first.

        Returns:
            List of error dicts with 'loc', 'msg', and 'type' keys.
        """
        errors: list[dict] = []

        for validator in self.__model_validators__:
            try:
                # Model validators should either modify self or return None
                result = validator(self)
                # Some validators might return a modified instance
                if result is not None and result is not self:
                    # This shouldn't happen with proper usage, but handle it gracefully
                    pass
            except (ValueError, TypeError) as e:
                errors.append(
                    {
                        "loc": ["body"],
                        "msg": str(e),
                        "type": "value_error",
                    }
                )

        return errors

    def validate(self: T) -> T:
        """
        Validate the current instance by re-running msgspec validation.

        This is useful when you create an instance directly with __init__()
        and want to validate it afterwards.

        Returns:
            A new validated instance

        Raises:
            ValidationError: If validation fails

        Example:
            # Direct creation skips Meta validation
            author = BenchAuthor(id=1, name="  John  ", email="BAD-EMAIL")

            # Validate afterwards (will raise ValidationError)
            author = author.validate()
        """
        # Convert to dict and back through msgspec to trigger full validation
        data = msgspec.structs.asdict(self)
        return msgspec.convert(data, type=self.__class__)

    @classmethod
    def _input_ignores_read_only(cls) -> bool:
        """Tell if input of this class can set a ``read_only`` field. Computed one time."""
        flag = cls.__read_only_input__
        if flag is None:
            flag = cls.__read_only_input__ = input_has_read_only_fields(cls)
        return flag

    @classmethod
    def _get_before_plan(cls) -> Any:
        """The plan for the before validators of this class and its nested serializers. Built one time."""
        plan = cls.__before_plan__
        if plan is PLAN_NOT_BUILT:
            plan = cls.__before_plan__ = before_plan(cls)
        return plan

    @classmethod
    def _validate_json_with_before(cls: type[T], plan: Any, json_data: str | bytes) -> T:
        """model_validate_json() of a class with a before validator: builtins, the plan, then convert."""
        data = run_before_validators(plan, msgspec.json.decode(json_data))
        token = RAW_VALIDATED.set(True)
        try:
            # str_keys: the keys of a JSON object are strings.
            return msgspec.convert(data, type=cls, str_keys=True)
        except MsgspecValidationError as e:
            if isinstance(e.__cause__, RequestValidationError):
                raise e.__cause__ from e
            if isinstance(data, dict):
                errors = cls._collect_msgspec_errors(data)
                if errors:
                    raise RequestValidationError(errors=errors) from e
            raise
        finally:
            RAW_VALIDATED.reset(token)

    @classmethod
    def _collect_msgspec_errors(cls: type[T], data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Collect all msgspec validation errors by validating each field individually.

        This is based on Litestar's approach: when msgspec.convert() fails with a
        ValidationError (which is fail-fast), we iterate through each field and
        validate it individually to collect ALL errors.

        Args:
            data: Dictionary of field values to validate

        Returns:
            List of error dicts with 'loc', 'msg', and 'type' keys
        """
        errors: list[dict[str, Any]] = []
        annotations = get_type_hints(cls, include_extras=True)
        rename_map = cls.__rename_map__
        fields = cls.__struct_fields__
        defaults = cls.__struct_defaults__
        default_offset = len(fields) - len(defaults)

        for idx, field_name in enumerate(fields):
            # rename_map.get() works for empty dict too (returns field_name as default)
            data_key = rename_map.get(field_name, field_name)
            if data_key not in data:
                default_idx = idx - default_offset
                is_required = default_idx < 0 or defaults[default_idx] is msgspec.NODEFAULT
                if is_required:
                    errors.append(
                        {
                            "loc": ("body", data_key),
                            "msg": f"Object missing required field `{data_key}`",
                            "type": "missing",
                        }
                    )
                continue

            try:
                field_type = annotations.get(field_name, Any)
                # Validate this field individually, as strictly as the conversion
                # of the whole object that failed.
                msgspec.convert(data[data_key], type=field_type)
            except MsgspecValidationError as e:
                # msgspec names the parent of a missing field in the path. The loc
                # of a missing field ends with its name, as for a top-level field.
                entries = msgspec_validation_error_to_dict(e, data_key)
                if len(entries) == 1 and entries[0]["type"] == "missing_field":
                    errors.append({"loc": ("body", *entries[0]["loc"]), "msg": entries[0]["msg"], "type": "missing"})
                    continue

                # The validators of a nested serializer give one entry for each error.
                if isinstance(e.__cause__, RequestValidationError):
                    errors.extend({**entry, "loc": ("body", *entry["loc"])} for entry in entries)
                    continue

                # The loc has one item for each step of the path, also for a list index.
                errors.append(
                    {
                        "loc": ("body", *entries[0]["loc"]),
                        "msg": str(e),
                        "type": "validation_error",
                    }
                )

        return errors

    @classmethod
    def model_validate(cls: type[T], data: dict[str, Any] | Any) -> T:
        """
        Validate data and create a serializer instance (Pydantic-style API).

        This triggers full msgspec validation (Meta constraints) plus custom validators.
        If validation fails, collects ALL errors (Litestar-style) before raising.

        Args:
            data: Dictionary or object to validate

        Returns:
            Validated Serializer instance

        Raises:
            RequestValidationError: If validation fails (with all errors collected)

        Example:
            data = {"id": 1, "name": "  John  ", "email": "JOHN@EXAMPLE.COM"}
            author = BenchAuthor.model_validate(data)
            # author.name == 'John' (stripped)
            # author.email == 'john@example.com' (lowercased)
        """
        if cls.__read_only_input__ is not False and not INPUT_MODE.get() and cls._input_ignores_read_only():
            token = INPUT_MODE.set(True)
            try:
                return cls.model_validate(data)
            finally:
                INPUT_MODE.reset(token)

        if cls.__before_plan__ is not None and not RAW_VALIDATED.get():
            plan = cls._get_before_plan()
            if plan is not None:
                data = run_before_validators(plan, data)
                token = RAW_VALIDATED.set(True)
                try:
                    return cls.model_validate(data)
                finally:
                    RAW_VALIDATED.reset(token)

        try:
            return msgspec.convert(data, type=cls)
        except MsgspecValidationError as e:
            # msgspec wraps the RequestValidationError of our validators. Raise it
            # with its error list, as model_validate_json() does.
            if isinstance(e.__cause__, RequestValidationError):
                raise e.__cause__ from e

            # Collect all errors by validating field-by-field (Litestar approach)
            if isinstance(data, dict):
                errors = cls._collect_msgspec_errors(data)
                if errors:
                    raise RequestValidationError(errors=errors) from e
            # Re-raise original error if we couldn't collect more details
            raise

    @classmethod
    def model_validate_json(cls: type[T], json_data: str | bytes) -> T:
        """
        Validate JSON string and create a serializer instance (Pydantic-style API).

        This triggers full msgspec validation (Meta constraints) plus custom validators.
        If validation fails, collects ALL errors (Litestar-style) before raising.

        Args:
            json_data: JSON string or bytes to validate

        Returns:
            Validated Serializer instance

        Raises:
            RequestValidationError: If validation fails (with all errors collected)

        Example:
            json_str = '{"id": 1, "name": "  John  ", "email": "JOHN@EXAMPLE.COM"}'
            author = BenchAuthor.model_validate_json(json_str)
            # author.name == 'John' (stripped)
            # author.email == 'john@example.com' (lowercased)
        """
        if cls.__read_only_input__ is not False and not INPUT_MODE.get() and cls._input_ignores_read_only():
            token = INPUT_MODE.set(True)
            try:
                return cls.model_validate_json(json_data)
            finally:
                INPUT_MODE.reset(token)

        if cls.__before_plan__ is not None and not RAW_VALIDATED.get():
            plan = cls._get_before_plan()
            if plan is not None:
                return cls._validate_json_with_before(plan, json_data)

        try:
            return msgspec.json.decode(json_data, type=cls)
        except MsgspecValidationError as e:
            # Check if the error was caused by our validators (RequestValidationError)
            # msgspec wraps exceptions from __post_init__ in ValidationError
            if isinstance(e.__cause__, RequestValidationError):
                raise e.__cause__ from e

            # Try to parse JSON first to get dict, then collect errors field-by-field
            try:
                data = msgspec.json.decode(json_data)
            except msgspec.DecodeError:
                # JSON is malformed, re-raise original validation error
                raise e from e

            if isinstance(data, dict):
                errors = cls._collect_msgspec_errors(data)
                if errors:
                    raise RequestValidationError(errors=errors) from e

            # Re-raise original error if we couldn't collect more details
            raise

    @classmethod
    def _ensure_from_model_ready(cls) -> None:
        """Ensure lazy field metadata has been collected before model extraction."""
        if not cls.__field_configs_collected__:
            cls._lazy_collect_field_configs()

    @classmethod
    def _ensure_dump_ready(cls) -> None:
        """Ensure lazy field metadata has been collected before dumping/views."""
        if not cls.__field_configs_collected__:
            cls._lazy_collect_field_configs()

    @classmethod
    def _build_dump_field_specs(cls) -> tuple[_DumpFieldSpec, ...]:
        """Pre-compute dump metadata for struct fields."""
        default_values = {
            field_name: _resolve_dump_default(default_value) for field_name, default_value in _iter_field_defaults(cls)
        }
        nested_fields = cls.__nested_fields__
        rename_map = cls.__rename_map__
        field_dumpers = compile_field_dumpers(cls, cls.__cached_type_hints__)
        specs: list[_DumpFieldSpec] = []

        for field_name in cls.__struct_fields__:
            if field_name in cls.__write_only_fields__:
                continue

            field_config = cls.__field_configs__.get(field_name)
            if field_config and field_config.exclude:
                continue

            specs.append(
                _DumpFieldSpec(
                    field_name=field_name,
                    output_key=rename_map.get(field_name, field_name),
                    nested=_get_model_output_nested(cls.__cached_type_hints__.get(field_name, Any))
                    if field_name in nested_fields
                    else None,
                    default_value=default_values.get(field_name, _MISSING),
                    dumper=field_dumpers.get(field_name),
                )
            )

        return tuple(specs)

    @classmethod
    def _get_dump_field_specs(
        cls,
        *,
        include_fields: frozenset[str] | None = None,
        exclude_fields: frozenset[str] | None = None,
    ) -> tuple[_DumpFieldSpec, ...]:
        """Return cached dump field specs for the requested field filter."""
        cls._ensure_dump_ready()
        if include_fields is None and exclude_fields is None:
            return cls._dump_field_specs

        cache_key = (include_fields, exclude_fields)
        specs = cls._dump_field_spec_cache.get(cache_key)
        if specs is None:
            specs = tuple(
                spec
                for spec in cls._dump_field_specs
                if (include_fields is None or spec.field_name in include_fields)
                and (exclude_fields is None or spec.field_name not in exclude_fields)
            )
            cls._dump_field_spec_cache[cache_key] = specs
        return specs

    @classmethod
    def _get_computed_dump_specs(
        cls,
        *,
        include_fields: frozenset[str] | None = None,
        exclude_fields: frozenset[str] | None = None,
    ) -> tuple[_ComputedDumpSpec, ...]:
        """Return cached computed-field specs for the requested field filter."""
        cls._ensure_dump_ready()
        if include_fields is None and exclude_fields is None:
            return cls._computed_dump_specs

        cache_key = (include_fields, exclude_fields)
        specs = cls._computed_dump_spec_cache.get(cache_key)
        if specs is None:
            specs = tuple(
                spec
                for spec in cls._computed_dump_specs
                if (include_fields is None or spec.field_name in include_fields)
                and (exclude_fields is None or spec.field_name not in exclude_fields)
            )
            cls._computed_dump_spec_cache[cache_key] = specs
        return specs

    @classmethod
    def _get_cached_view(
        cls: type[T],
        *,
        include_fields: frozenset[str] | None = None,
        exclude_fields: frozenset[str] | None = None,
    ) -> SerializerView[T]:
        """Return a cached SerializerView for a given field-selection pair."""
        cls._ensure_dump_ready()
        cache_key = (include_fields, exclude_fields)
        cached = cls._serializer_view_cache.get(cache_key)
        if cached is None:
            cached = SerializerView(cls, include_fields=include_fields, exclude_fields=exclude_fields)
            cls._serializer_view_cache[cache_key] = cached
        return cast("SerializerView[T]", cached)

    @classmethod
    def _relation_loading_hint(cls, relation: _DjangoRelationInfo) -> str:
        """Return the recommended Django ORM hint for preloading a relation."""
        if relation.kind == "many":
            return f"prefetch_related('{relation.name}')"
        return f"select_related('{relation.name}')"

    @classmethod
    def _raise_unloaded_relation_error(
        cls,
        *,
        field_name: str,
        relation: _UnloadedRelation,
        instance: Any,
    ) -> None:
        """Raise a descriptive error when sync from_model() would need ORM I/O."""
        raise UnloadedRelationError(
            f"{cls.__name__}.{field_name} requires loaded relation '{relation.source}' on "
            f"{instance.__class__.__name__} when using from_model(). Preload it with "
            f"{cls._relation_loading_hint(_DjangoRelationInfo(relation.relation_name, relation.relation_kind, relation.relation_name, None))} "
            f"or use await {cls.__name__}.afrom_model(instance)."
        )

    @staticmethod
    def _normalize_many_relation_value(value: Any) -> list[Any]:
        """Normalize prefetched many-relation values into a concrete list."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, (BaseManager, QuerySet)):
            return list(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return list(value)
        return [value]

    @classmethod
    def _get_loaded_relation_sync(cls, obj: DjangoModel, relation: _DjangoRelationInfo) -> Any:
        """Read a relation only if Django already has it cached on the instance."""
        state = getattr(obj, "_state", None)
        fields_cache = getattr(state, "fields_cache", {}) if state is not None else {}

        if relation.kind in {"forward_one", "reverse_one"}:
            for key in (relation.cache_name, relation.name):
                if key in fields_cache:
                    return fields_cache[key]
                if key in getattr(obj, "__dict__", {}):
                    return obj.__dict__[key]
            return _MISSING

        prefetch_cache = getattr(obj, "_prefetched_objects_cache", None)
        if isinstance(prefetch_cache, dict):
            for key in (relation.cache_name, relation.name):
                if key in prefetch_cache:
                    return cls._normalize_many_relation_value(prefetch_cache[key])
        return _MISSING

    @staticmethod
    def _cache_loaded_relation(obj: DjangoModel, relation: _DjangoRelationInfo, value: Any) -> None:
        """Store async-loaded relations back on the Django instance for reuse."""
        if relation.kind == "many":
            cache = getattr(obj, "_prefetched_objects_cache", None)
            if cache is None:
                cache = {}
                obj._prefetched_objects_cache = cache
            cache[relation.cache_name] = list(value) if isinstance(value, list) else value
            return

        state = getattr(obj, "_state", None)
        if state is not None:
            fields_cache = getattr(state, "fields_cache", None)
            if fields_cache is not None:
                fields_cache[relation.cache_name] = value
                return
        obj.__dict__[relation.cache_name] = value

    @classmethod
    async def _load_relation_async(cls, obj: DjangoModel, relation: _DjangoRelationInfo) -> Any:
        """Load a relation through Django's async ORM when afrom_model() is used."""
        related_model = relation.related_model
        if related_model is None:
            return None
        related_model_cls = cast(Any, related_model)

        if relation.kind == "forward_one":
            if relation.attname is None:
                return None
            related_id = getattr(obj, relation.attname, None)
            if related_id is None:
                return None
            value = await related_model_cls.objects.aget(pk=related_id)
            cls._cache_loaded_relation(obj, relation, value)
            return value

        if relation.kind == "reverse_one":
            if relation.reverse_query_attname is None:
                return None
            try:
                value = await related_model_cls.objects.aget(**{relation.reverse_query_attname: obj.pk})
            except related_model_cls.DoesNotExist:
                value = None
            cls._cache_loaded_relation(obj, relation, value)
            return value

        manager = getattr(obj, relation.name)
        value = [item async for item in manager.all()]
        cls._cache_loaded_relation(obj, relation, value)
        return value

    @classmethod
    async def _get_or_load_relation_async(
        cls,
        obj: DjangoModel,
        relation: _DjangoRelationInfo,
        *,
        relation_tasks: _RelationTaskCache,
    ) -> Any:
        """Reuse in-flight relation loads so gathered field extraction doesn't duplicate queries."""
        loaded_value = cls._get_loaded_relation_sync(obj, relation)
        if loaded_value is not _MISSING:
            return loaded_value

        cache_key = (id(obj), relation.cache_name)
        task = relation_tasks.get(cache_key)
        if task is None:
            task = asyncio.create_task(cls._load_relation_async(obj, relation))
            relation_tasks[cache_key] = task

        try:
            return await task
        except Exception:
            if relation_tasks.get(cache_key) is task:
                relation_tasks.pop(cache_key, None)
            raise

    @classmethod
    def _resolve_source_sync(
        cls,
        obj: Any,
        source: str,
        *,
        nested_output: _ModelOutputNested | None,
    ) -> Any:
        """Resolve a dot-path without triggering ORM I/O."""
        parts = source.split(".")
        current = obj

        for idx, part in enumerate(parts):
            if current is None:
                return None

            is_last = idx == len(parts) - 1
            if isinstance(current, DjangoModel) and part in current.__dict__:
                current = current.__dict__[part]
                continue

            relation = _get_django_relation_info(current.__class__, part) if isinstance(current, DjangoModel) else None

            if relation is not None:
                if (
                    is_last
                    and nested_output is None
                    and relation.kind == "forward_one"
                    and relation.attname is not None
                ):
                    return getattr(current, relation.attname, None)

                loaded_value = cls._get_loaded_relation_sync(current, relation)
                if loaded_value is _MISSING:
                    return _UnloadedRelation(
                        source=".".join(parts[: idx + 1]),
                        relation_name=relation.name,
                        relation_kind=relation.kind,
                    )
                current = loaded_value
                continue

            if isinstance(current, dict):
                if part not in current:
                    return _MISSING
                current = current[part]
                continue

            if isinstance(current, list):
                return _MISSING

            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)

        return current

    @classmethod
    async def _resolve_source_async(
        cls,
        obj: Any,
        source: str,
        *,
        nested_output: _ModelOutputNested | None,
        relation_tasks: _RelationTaskCache,
    ) -> Any:
        """Resolve a dot-path, loading relations through Django's async ORM as needed."""
        parts = source.split(".")
        current = obj

        for idx, part in enumerate(parts):
            if current is None:
                return None

            is_last = idx == len(parts) - 1
            if isinstance(current, DjangoModel) and part in current.__dict__:
                current = current.__dict__[part]
                continue

            relation = _get_django_relation_info(current.__class__, part) if isinstance(current, DjangoModel) else None

            if relation is not None:
                if (
                    is_last
                    and nested_output is None
                    and relation.kind == "forward_one"
                    and relation.attname is not None
                ):
                    return getattr(current, relation.attname, None)

                loaded_value = cls._get_loaded_relation_sync(current, relation)
                if loaded_value is _MISSING:
                    loaded_value = await cls._get_or_load_relation_async(
                        current,
                        relation,
                        relation_tasks=relation_tasks,
                    )
                current = loaded_value
                continue

            if isinstance(current, dict):
                if part not in current:
                    return _MISSING
                current = current[part]
                continue

            if isinstance(current, list):
                return _MISSING

            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)

        return current

    @classmethod
    def _convert_regular_from_model_value(cls, value: Any, *, field_name: str) -> Any:
        """Convert ORM-ish values into regular serializer output values."""
        if value.__class__ in _MODEL_VALUE_NOOP_TYPES:
            return value
        if isinstance(value, Choices):
            # Django keeps create()/acreate() choices as enum members in memory until
            # the instance is reloaded from the database. Normalize them here so
            # dump()/dump_json() emit the primitive value without refresh_from_db().
            return value.value
        if isinstance(value, DjangoModel):
            return value.pk
        if isinstance(value, (BaseManager, QuerySet)):
            raise SerializationError(
                f"{cls.__name__}.{field_name} still contains {value.__class__.__name__}; "
                "from_model() must resolve Django managers/querysets before dump()."
            )
        if isinstance(value, list) and value and isinstance(value[0], DjangoModel):
            return [item.pk for item in value]
        return value

    @classmethod
    def _convert_nested_from_model_value(
        cls,
        value: Any,
        *,
        nested_output: _ModelOutputNested,
        _depth: int,
        max_depth: int,
    ) -> Any:
        """Convert nested model values for sync from_model()."""
        serializer_class = nested_output.serializer_class

        if value is None:
            return None

        if nested_output.many:
            items = cls._normalize_many_relation_value(value)

            result = []
            for item in items:
                if isinstance(item, serializer_class):
                    result.append(item)
                elif isinstance(item, DjangoModel):
                    result.append(serializer_class.from_model(item, _depth=_depth + 1, max_depth=max_depth))
                elif isinstance(item, dict):
                    result.append(serializer_class(**item))
                else:
                    result.append(item)
            return result

        if isinstance(value, serializer_class):
            return value
        if isinstance(value, DjangoModel):
            return serializer_class.from_model(value, _depth=_depth + 1, max_depth=max_depth)
        if isinstance(value, dict):
            return serializer_class(**value)
        return value

    @classmethod
    async def _convert_nested_from_model_value_async(
        cls,
        value: Any,
        *,
        nested_output: _ModelOutputNested,
        _depth: int,
        max_depth: int,
    ) -> Any:
        """Convert nested model values for async afrom_model()."""
        serializer_class = nested_output.serializer_class

        if value is None:
            return None

        if nested_output.many:
            items = cls._normalize_many_relation_value(value)

            async def convert_item(item: Any) -> Any:
                if isinstance(item, serializer_class):
                    return item
                if isinstance(item, DjangoModel):
                    return await serializer_class.afrom_model(item, _depth=_depth + 1, max_depth=max_depth)
                if isinstance(item, dict):
                    return serializer_class(**item)
                return item

            return list(await asyncio.gather(*(convert_item(item) for item in items)))

        if isinstance(value, serializer_class):
            return value
        if isinstance(value, DjangoModel):
            return await serializer_class.afrom_model(value, _depth=_depth + 1, max_depth=max_depth)
        if isinstance(value, dict):
            return serializer_class(**value)
        return value

    @classmethod
    def _extract_model_field_sync(
        cls,
        instance: Any,
        spec: _ModelFieldSpec,
        *,
        _depth: int,
        max_depth: int,
    ) -> Any:
        """Extract one serializer field from a model/object without ORM I/O."""
        source = spec.source or spec.field_name
        value = cls._resolve_source_sync(instance, source, nested_output=spec.nested)

        if value is _MISSING:
            return _USE_DEFAULT if spec.has_default else _MISSING

        if isinstance(value, _UnloadedRelation):
            if spec.has_default:
                return _USE_DEFAULT
            cls._raise_unloaded_relation_error(field_name=spec.field_name, relation=value, instance=instance)

        if spec.nested is not None:
            return cls._convert_nested_from_model_value(
                value,
                nested_output=spec.nested,
                _depth=_depth,
                max_depth=max_depth,
            )

        return cls._convert_regular_from_model_value(value, field_name=spec.field_name)

    @classmethod
    async def _extract_model_field_async(
        cls,
        instance: Any,
        spec: _ModelFieldSpec,
        *,
        _depth: int,
        max_depth: int,
        relation_tasks: _RelationTaskCache,
    ) -> Any:
        """Extract one serializer field from a model/object with async ORM support."""
        source = spec.source or spec.field_name
        value = await cls._resolve_source_async(
            instance,
            source,
            nested_output=spec.nested,
            relation_tasks=relation_tasks,
        )

        if value is _MISSING:
            return _USE_DEFAULT if spec.has_default else _MISSING

        if spec.nested is not None:
            return await cls._convert_nested_from_model_value_async(
                value,
                nested_output=spec.nested,
                _depth=_depth,
                max_depth=max_depth,
            )

        return cls._convert_regular_from_model_value(value, field_name=spec.field_name)

    @classmethod
    def from_model(
        cls: type[T],
        instance: Model,
        *,
        _depth: int = 0,
        max_depth: int = 10,
    ) -> T:
        """
        Create a serializer instance from a Django model instance.

        Args:
            instance: A Django model instance
            _depth: Internal - current recursion depth
            max_depth: Maximum recursion depth to prevent runaway recursion (default: 10)

        Returns:
            A new Serializer instance with fields populated from the model

        Raises:
            ValueError: If max_depth exceeded (indicates deeply nested or circular references)

        Note:
            Circular nested serializers (e.g., Author.posts -> Post.author -> Author.posts)
            are not recommended for API design. Use separate serializers with ID-only fields
            for reverse relationships. Django's ORM typically prevents infinite recursion
            through select_related/prefetch_related, but max_depth provides a safety net.

        Example:
            user = await User.objects.aget(id=1)
            user_data = UserPublicSerializer.from_model(user)
        """
        # Safety: Prevent runaway recursion from deeply nested or circular relationships
        if _depth > max_depth:
            raise ValueError(
                f"Maximum recursion depth ({max_depth}) exceeded in from_model(). "
                f"This usually indicates overly deep nesting or circular references. "
                f"Current serializer: {cls.__name__}, instance: {instance.__class__.__name__}(pk={instance.pk}). "
                f"Consider using separate serializers with ID-only fields for deeply nested relationships."
            )

        cls._ensure_from_model_ready()

        data = {}
        for spec in cls.__model_field_specs__:
            value = cls._extract_model_field_sync(instance, spec, _depth=_depth, max_depth=max_depth)
            if value is _MISSING or value is _USE_DEFAULT:
                continue
            data[spec.field_name] = value

        return cls(**data)

    @classmethod
    def loading_plan(cls, model_cls: type[Model]) -> LoadingPlan:
        """The ORM loads (select_related/prefetch_related/annotate) this serializer needs from ``model_cls``."""
        return build_loading_plan(cls, model_cls)

    @classmethod
    def _prepare_queryset(cls, queryset: QuerySet) -> QuerySet:
        """Apply this serializer's loading plan to ``queryset`` (additive, idempotent)."""
        return build_loading_plan(cls, queryset.model).apply(queryset)

    @classmethod
    def from_models(cls: type[T], objects: QuerySet | Iterable[Model]) -> list[T]:
        """Build one serializer instance per model in ``objects``.

        A QuerySet is first prepared with this serializer's loading plan —
        FK/O2O → ``select_related``, M2M/reverse → ``prefetch_related`` (nested
        serializers recurse), ``Config.annotations`` → ``annotate`` — so the
        whole batch costs a bounded number of queries; loads the caller already
        requested are kept. Any other iterable of instances is used as-is.

        Bolt does this automatically for routes annotated ``list[ThisSerializer]``
        that return a QuerySet; call it when you evaluate the queryset yourself.
        Use :meth:`afrom_models` from async code.
        """
        if isinstance(objects, QuerySet):
            objects = cls._prepare_queryset(objects)
        return [cls.from_model(obj) for obj in objects]

    @classmethod
    async def afrom_models(cls: type[T], objects: QuerySet | Iterable[Model]) -> list[T]:
        """Async :meth:`from_models`: iterates the prepared QuerySet with the async ORM.

        Rows are converted with the sync :meth:`from_model`, so relations the
        loading plan cannot cover are *not* lazily loaded (unlike
        :meth:`afrom_model`): a required nested field left unloaded raises
        ``UnloadedRelationError`` — add the load to the queryset instead.
        """
        if isinstance(objects, QuerySet):
            return [cls.from_model(obj) async for obj in cls._prepare_queryset(objects)]
        return [cls.from_model(obj) for obj in objects]

    @classmethod
    async def afrom_model(
        cls: type[T],
        instance: Model,
        *,
        _depth: int = 0,
        max_depth: int = 10,
    ) -> T:
        """Async variant of from_model() that may lazy-load relations safely."""
        if _depth > max_depth:
            raise ValueError(
                f"Maximum recursion depth ({max_depth}) exceeded in afrom_model(). "
                f"This usually indicates overly deep nesting or circular references. "
                f"Current serializer: {cls.__name__}, instance: {instance.__class__.__name__}(pk={instance.pk}). "
                f"Consider using separate serializers with ID-only fields for deeply nested relationships."
            )

        cls._ensure_from_model_ready()

        relation_tasks: _RelationTaskCache = {}
        values = await asyncio.gather(
            *(
                cls._extract_model_field_async(
                    instance,
                    spec,
                    _depth=_depth,
                    max_depth=max_depth,
                    relation_tasks=relation_tasks,
                )
                for spec in cls.__model_field_specs__
            )
        )

        data = {}
        for spec, value in zip(cls.__model_field_specs__, values, strict=False):
            if value is _MISSING or value is _USE_DEFAULT:
                continue
            data[spec.field_name] = value

        return cls(**data)

    def to_dict(self, *, exclude_unset: bool = False) -> dict[str, Any]:
        """
        Convert serializer to a dictionary.

        Args:
            exclude_unset: If True, exclude fields that hold msgspec.UNSET

        Returns:
            Dictionary representation of the serializer

        Example:
            user_data = UserCreateSerializer(...)
            user = await User.objects.acreate(**user_data.to_dict())
        """
        result = {}
        for field_name in self.__struct_fields__:
            value = getattr(self, field_name)
            if exclude_unset and value is msgspec.UNSET:
                continue
            result[field_name] = value
        return result

    def to_model(self, model_class: type[Model]) -> Model:
        """
        Create a Django model instance (unsaved) from the serializer.

        Args:
            model_class: The Django model class to instantiate

        The instance does not get the value of a ``read_only`` field.

        Returns:
            An unsaved model instance

        Example:
            user_data = UserCreateSerializer(...)
            user = user_data.to_model(User)
            await user.asave()

        A ForeignKey or OneToOneField field can hold the primary key of the
        related row (``author: int``) or a model instance.
        """
        read_only = self.__read_only_fields__
        data = {name: value for name, value in self.to_dict().items() if name not in read_only}
        relation_attnames = _RELATION_ATTNAMES.get(model_class)
        if relation_attnames is None:
            relation_attnames = _relation_attnames(model_class)
        for name, attname in relation_attnames.items():
            # A primary key goes to the column attribute (author_id). A model
            # instance, and the dict of a nested serializer, stay on the relation.
            if name in data and not isinstance(data[name], (DjangoModel, dict)):
                data[attname] = data.pop(name)
        return model_class(**data)

    def update_instance(self: T, instance: Model) -> Model:
        """
        Update a Django model instance with values from this serializer.

        Only updates fields that are present in the serializer. When omit_defaults=True,
        only updates fields that differ from their default values. Does not update
        a ``read_only`` field.

        Args:
            instance: The model instance to update

        Returns:
            The updated model instance (not saved)

        Example:
            user = await User.objects.aget(id=1)
            user_update = UserUpdateSerializer(username="new_name")
            updated_user = user_update.update_instance(user)
            await updated_user.asave()

        A ForeignKey or OneToOneField field can hold the primary key of the
        related row (``author: int``) or a model instance.
        """
        relation_attnames = _RELATION_ATTNAMES.get(type(instance))
        if relation_attnames is None:
            relation_attnames = _relation_attnames(type(instance))
        # Check if omit_defaults is enabled
        omit_defaults = self.__struct_config__.omit_defaults

        # Get field defaults if omit_defaults is enabled
        default_values = dict(_iter_field_defaults(type(self))) if omit_defaults else {}

        read_only = self.__read_only_fields__

        for field_name in self.__struct_fields__:
            if field_name in read_only:
                continue

            value = getattr(self, field_name)

            # Skip fields that are at their default value if omit_defaults is True
            if omit_defaults and field_name in default_values and value == default_values[field_name]:
                continue

            # A primary key goes to the column attribute (author_id), as in to_model().
            if field_name in relation_attnames and not isinstance(value, (DjangoModel, dict)):
                field_name = relation_attnames[field_name]
            setattr(instance, field_name, value)
        return instance

    class Config:
        """Configuration for Serializer. Can be overridden in subclasses.

        Note: Named 'Config' (not 'Meta') to avoid conflict with msgspec.Meta
        used for type constraints in Annotated[type, Meta(...)].
        """

        model: type[Model] | None = None
        """Associated Django model class (optional)"""

        write_only: set[str] = set()
        """Field names that should only be accepted on input, not returned"""

        read_only: set[str] = set()
        """Field names that input cannot set. Each field gets its default and must have one."""

        field_sets: dict[str, list[str]] = {}
        """
        Named field sets for dynamic field selection.

        Example:
            class Config:
                field_sets = {
                    "list": ["id", "name", "email"],
                    "detail": ["id", "name", "email", "created_at", "posts"],
                    "create": ["name", "email", "password"],
                }
        """

    # -------------------------------------------------------------------------
    # Dynamic Field Selection Methods
    # -------------------------------------------------------------------------

    @classmethod
    def subset(cls: type[T], *fields: str, name: str | None = None) -> type[T]:
        """
        Create a new Serializer class with only the specified fields.

        This creates a TRUE subclass (not a view) that can be used as a type
        annotation, response_model, or anywhere a Serializer type is expected.

        Args:
            *fields: Field names to include (struct fields and computed fields)
            name: Optional name for the new class. If not provided, auto-generates one.

        Returns:
            A new Serializer class with only the specified fields

        Example:
            class UserSerializer(Serializer):
                id: int
                name: str
                email: str
                password: str
                created_at: datetime

                class Config:
                    write_only = {"password"}

                @computed_field
                def display_name(self) -> str:
                    return f"@{self.name}"

            # Create type-safe mini serializers
            UserMiniSerializer = UserSerializer.subset("id", "name")
            UserPublicSerializer = UserSerializer.subset("id", "name", "email", "display_name")

            # Use as response_model (type-safe!)
            @api.get("/users/{id}", response_model=UserMiniSerializer)
            async def get_user(id: int) -> UserMiniSerializer:
                user = await User.objects.aget(id=id)
                return UserMiniSerializer.from_model(user)

            # Or use from_parent to convert existing instances
            full_user = UserSerializer.from_model(user)
            mini_user = UserMiniSerializer.from_parent(full_user)

        Note:
            - Computed fields are included if their name is in the fields list
            - write_only fields from Meta are automatically excluded from subsets
            - The subset inherits validators for included fields
            - The subset keeps the rename, omit_defaults, forbid_unknown_fields and frozen
              options of the parent. It does not keep the tag
            - The subset inherits a model validator when it has each field that the
              validator names (``self.low``). A model validator that names a field
              outside the subset does not run for the subset. The same applies to
              a method or a class variable of the parent that is not in the subset
            - Always define subsets at module level, not inside view functions
        """
        fields_set = frozenset(fields)

        # Get type hints for the original class
        hints = cls.__cached_type_hints__

        # Build annotations for the new class (only struct fields, not computed)
        new_annotations: dict[str, Any] = {}
        struct_fields_to_include: list[str] = []

        for field_name in cls.__struct_fields__:
            if field_name in fields_set:
                if field_name in hints:
                    new_annotations[field_name] = hints[field_name]
                struct_fields_to_include.append(field_name)

        # Build class dict
        class_name = name or f"{cls.__name__}Subset_{hash(fields_set) & 0xFFFFFF:06x}"
        class_dict: dict[str, Any] = {
            "__annotations__": new_annotations,
            "__module__": cls.__module__,
            "__qualname__": class_name,
        }

        # Handle defaults: msgspec defaults are aligned from the END
        # We need to set defaults for fields that have them in the parent
        parent_defaults = cls.__struct_defaults__
        parent_fields = cls.__struct_fields__
        num_parent_fields = len(parent_fields)
        num_defaults = len(parent_defaults)

        # Build parent field -> default mapping
        parent_default_map: dict[str, Any] = {}
        for i, default_val in enumerate(parent_defaults):
            field_idx = num_parent_fields - num_defaults + i
            if field_idx >= 0:
                parent_default_map[parent_fields[field_idx]] = default_val

        # Set defaults on the new class for fields that have them. A field with
        # field() and no default keeps its config.
        required_field_configs = cls.__required_field_configs__
        for field_name in struct_fields_to_include:
            if field_name in parent_default_map:
                class_dict[field_name] = parent_default_map[field_name]
            elif field_name in required_field_configs:
                class_dict[field_name] = _FieldMarker(config=required_field_configs[field_name])

        # Copy computed field methods that are in the fields list
        computed_to_include: list[str] = []
        for field_name, config in cls.__computed_fields__.items():
            if field_name in fields_set:
                method = getattr(cls, config.method_name)
                class_dict[config.method_name] = method
                computed_to_include.append(field_name)

        # Copy field validators for included fields
        for field_name, validators in cls.__field_validators__.items():
            if field_name in fields_set:
                for index, validator in enumerate(validators):
                    class_dict[f"_validator_{field_name}_{index}"] = validator

        # Copy Config with adjusted field_sets (only include subsets of our fields)
        parent_meta = getattr(cls, "Config", None)
        if parent_meta:

            class Config:
                pass

            # Copy relevant Config attributes
            if hasattr(parent_meta, "model"):
                Config.model = parent_meta.model  # type: ignore
            # Adjust write_only/read_only to only include fields we have
            if hasattr(parent_meta, "write_only"):
                Config.write_only = parent_meta.write_only & fields_set  # type: ignore
            if hasattr(parent_meta, "read_only"):
                Config.read_only = parent_meta.read_only & fields_set  # type: ignore
            class_dict["Config"] = Config

        # Copy each model validator that the subset can run. A validator that
        # names a parent attribute outside the subset reads an attribute that
        # the subset does not have, so it stays out. Such an attribute is a
        # field, a method or a class variable of the parent.
        for validator in cls.__model_validators__:
            if not any(
                hasattr(cls, name)
                and name not in fields_set
                and name not in class_dict
                and not hasattr(Serializer, name)
                for name in _names_in_code(validator)
            ):
                class_dict[validator.__name__] = validator

        # Create the new Serializer subclass. We intentionally do NOT inherit
        # the parent's tag — a subset is a distinct schema (different fields)
        # and reusing the parent's tag would create duplicate-tag collisions
        # in any tagged union containing both. Callers who need a tagged
        # subset can subclass with an explicit `tag=...`.
        # The subset keeps the wire names and the struct options of the parent.
        struct_config = cls.__struct_config__
        new_cls: type[T] = type(  # type: ignore
            class_name,
            (Serializer,),
            class_dict,
            kw_only=True,
            rename={name: cls.__rename_map__[name] for name in struct_fields_to_include if name in cls.__rename_map__}
            or None,
            omit_defaults=struct_config.omit_defaults,
            forbid_unknown_fields=struct_config.forbid_unknown_fields,
            frozen=struct_config.frozen,
        )

        # Add from_parent class method
        @classmethod
        def from_parent(new_cls_ref: type[T], instance: Serializer) -> T:
            """Create instance from a parent serializer instance."""
            data = {}
            for field_name in new_cls_ref.__struct_fields__:
                if hasattr(instance, field_name):
                    data[field_name] = getattr(instance, field_name)
            return new_cls_ref(**data)

        cast(Any, new_cls).from_parent = from_parent

        return new_cls

    @classmethod
    def fields(cls: type[T], field_set: str, *, name: str | None = None) -> type[T]:
        """
        Create a new Serializer class from a predefined field set.

        This is a convenience method that combines Meta.field_sets with subset().
        The result is a type-safe Serializer class.

        Args:
            field_set: Name of the field set defined in Meta.field_sets
            name: Optional name for the new class

        Returns:
            A new Serializer class with the fields from the field set

        Raises:
            TypeError: If field_set is not a non-empty string
            ValueError: If the field set name is not defined

        Example:
            class UserSerializer(Serializer):
                id: int
                name: str
                email: str
                password: str

                class Config:
                    field_sets = {
                        "list": ["id", "name"],
                        "detail": ["id", "name", "email"],
                    }

            # Create type-safe serializers from field sets
            UserListSerializer = UserSerializer.fields("list")
            UserDetailSerializer = UserSerializer.fields("detail")

            # Use as response_model
            @api.get("/users", response_model=list[UserListSerializer])
            async def list_users() -> list[UserListSerializer]:
                ...
        """
        # Validate field_set parameter
        if not isinstance(field_set, str) or not field_set:
            raise TypeError("field_set must be a non-empty string")

        field_sets = getattr(cls, "__field_sets__", {})
        if field_set not in field_sets:
            available = ", ".join(field_sets.keys()) if field_sets else "none defined"
            raise ValueError(
                f"Field set '{field_set}' not found in {cls.__name__}.Config.field_sets. "
                f"Available field sets: {available}"
            )

        fields_list = field_sets[field_set]
        class_name = name or f"{cls.__name__}{field_set.title()}"
        return cls.subset(*fields_list, name=class_name)

    @classmethod
    def only(cls: type[T], *fields: str) -> SerializerView[T]:
        """
        Create a view that only includes the specified fields during serialization.

        This does NOT create a new serializer class - it returns a view object
        that wraps the serializer and filters fields during dump().

        Args:
            *fields: Field names to include in output

        Returns:
            SerializerView that can be used like the serializer but with filtered fields

        Example:
            # Only include id and name in output
            UserSerializer.only("id", "name").dump(user)
            # Returns: {"id": 1, "name": "John"}

            # Chain with dump_many for lists
            UserSerializer.only("id", "email").dump_many(users)
        """
        return cls._get_cached_view(include_fields=frozenset(fields))

    @classmethod
    def exclude(cls: type[T], *fields: str) -> SerializerView[T]:
        """
        Create a view that excludes the specified fields during serialization.

        This does NOT create a new serializer class - it returns a view object
        that wraps the serializer and filters fields during dump().

        Args:
            *fields: Field names to exclude from output

        Returns:
            SerializerView that can be used like the serializer but with filtered fields

        Example:
            # Exclude password from output
            UserSerializer.exclude("password", "secret_key").dump(user)

            # Chain with dump_many for lists
            UserSerializer.exclude("internal_notes").dump_many(users)
        """
        return cls._get_cached_view(exclude_fields=frozenset(fields))

    @classmethod
    def use(cls: type[T], field_set: str) -> SerializerView[T]:
        """
        Create a view using a predefined field set from Config.field_sets.

        This allows you to define common field combinations once and reuse them.

        Args:
            field_set: Name of the field set defined in Config.field_sets

        Returns:
            SerializerView configured with the predefined field set

        Raises:
            ValueError: If the field set name is not defined

        Example:
            class UserSerializer(Serializer):
                id: int
                name: str
                email: str
                password: str = field(write_only=True)
                created_at: datetime

                class Config:
                    field_sets = {
                        "list": ["id", "name"],
                        "detail": ["id", "name", "email", "created_at"],
                    }

            # Use predefined field sets
            UserSerializer.use("list").dump_many(users)
            UserSerializer.use("detail").dump(user)
        """
        field_sets = getattr(cls, "__field_sets__", {})
        if field_set not in field_sets:
            available = ", ".join(field_sets.keys()) if field_sets else "none defined"
            raise ValueError(
                f"Field set '{field_set}' not found in {cls.__name__}.Config.field_sets. "
                f"Available field sets: {available}"
            )
        return cls._get_cached_view(include_fields=frozenset(field_sets[field_set]))

    # -------------------------------------------------------------------------
    # Dump Methods (Serialization)
    # -------------------------------------------------------------------------

    def dump(
        self,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> dict[str, Any]:
        """
        Serialize this instance to a dictionary.

        This method respects read_only/write_only field configurations and
        includes computed fields in the output.

        Args:
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            Dictionary representation of the serializer

        Example:
            user = UserSerializer(id=1, name="John", email=None)
            user.dump()  # {"id": 1, "name": "John", "email": None}
            user.dump(exclude_none=True)  # {"id": 1, "name": "John"}
        """
        # FAST PATH: use msgspec native methods (significantly faster than Python iteration)
        cls = self.__class__
        cls._ensure_dump_ready()
        if cls.__dump_fast_path__ and not exclude_none and not exclude_defaults and not exclude_unset and not by_alias:
            if cls.__orm_state_check_fields__:
                self._ensure_dumpable_orm_state(cls.__orm_state_check_fields__)
            # asdict is cheapest when the keys are the field names. For a rename,
            # pair the encoded names with the values. The values stay as they are,
            # as on the slow path.
            if cls.__tag_value__ is not None:
                result = {cls.__tag_field__: cls.__tag_value__}
                result.update(zip(cls.__struct_encode_fields__, msgspec_structs.astuple(self), strict=True))
                return result
            if cls.__has_rename__:
                return dict(zip(cls.__struct_encode_fields__, msgspec_structs.astuple(self), strict=True))
            return msgspec_structs.asdict(self)
        # _dump_impl gives the same dict, so a mutant of this check changes only the cost.
        if not exclude_none and not exclude_defaults and not exclude_unset and not by_alias:  # pragma: no mutate
            return cls.__dump_function__(self)

        # SLOW PATH: Need special handling.
        return self._dump_impl(
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
            include_fields=None,
            exclude_fields=None,
        )

    def _dump_impl(
        self,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
        include_fields: frozenset[str] | None = None,
        exclude_fields: frozenset[str] | None = None,
        field_specs: tuple[_DumpFieldSpec, ...] | None = None,
        computed_specs: tuple[_ComputedDumpSpec, ...] | None = None,
    ) -> dict[str, Any]:
        """Internal implementation of dump with field filtering."""
        cls = self.__class__
        cls._ensure_dump_ready()
        field_specs = (
            field_specs
            if field_specs is not None
            else cls._get_dump_field_specs(include_fields=include_fields, exclude_fields=exclude_fields)
        )
        computed_specs = (
            computed_specs
            if computed_specs is not None
            else cls._get_computed_dump_specs(include_fields=include_fields, exclude_fields=exclude_fields)
        )

        result: dict[str, Any] = {}

        # Add msgspec tag field if configured
        if cls.__tag_value__ is not None:
            result[cls.__tag_field__] = cls.__tag_value__

        # Local reference to getattr for micro-optimization
        _getattr = getattr

        for spec in field_specs:
            value = _getattr(self, spec.field_name)

            # Skip None values if exclude_none
            if exclude_none and value is None:
                continue

            if exclude_unset and value is msgspec.UNSET:
                continue

            if exclude_defaults and spec.default_value is not _MISSING and value == spec.default_value:
                continue

            # The wire name of the field: its alias, or the name from the rename option.
            output_key = spec.output_key

            if isinstance(value, (BaseManager, QuerySet)):
                raise SerializationError(
                    f"{cls.__name__}.{spec.field_name} contains {value.__class__.__name__}. "
                    "Serialize Django relations with from_model()/afrom_model() before dump()."
                )

            if spec.nested is not None:
                if spec.nested.many:
                    if isinstance(value, list) and value and isinstance(value[0], Serializer):
                        result[output_key] = [
                            item.dump(
                                exclude_none=exclude_none,
                                exclude_unset=exclude_unset,
                                exclude_defaults=exclude_defaults,
                                by_alias=by_alias,
                            )
                            for item in value
                        ]
                    else:
                        result[output_key] = value
                elif isinstance(value, Serializer):
                    result[output_key] = value.dump(
                        exclude_none=exclude_none,
                        exclude_unset=exclude_unset,
                        exclude_defaults=exclude_defaults,
                        by_alias=by_alias,
                    )
                else:
                    result[output_key] = value
            elif spec.dumper is not None:
                result[output_key] = spec.dumper(value)
            else:
                result[output_key] = value

        # Add computed fields
        for spec in computed_specs:
            method = _getattr(self, spec.method_name, None)
            if method is not None:
                value = method()

                if exclude_none and value is None:
                    continue

                result[spec.field_name] = value

        return result

    def _ensure_dumpable_orm_state(self, field_names: tuple[str, ...] | None = None) -> None:
        """Fail fast if ORM manager/queryset objects leaked into serializer state."""
        cls = self.__class__
        for field_name in field_names or cls.__struct_fields__:
            value = getattr(self, field_name)
            if isinstance(value, (BaseManager, QuerySet)):
                raise SerializationError(
                    f"{cls.__name__}.{field_name} contains {value.__class__.__name__}. "
                    "Serialize Django relations with from_model()/afrom_model() before dump()."
                )

    def dump_json(
        self,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> bytes:
        """
        Serialize this instance to JSON bytes.

        Uses msgspec for fast JSON encoding.

        Args:
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            JSON bytes representation
        """
        data = self.dump(
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
        )
        return _json.encode(data)

    @classmethod
    def dump_many(
        cls: type[T],
        instances: Iterable[T],
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Serialize multiple instances to a list of dictionaries.

        Args:
            instances: Iterable of serializer instances to dump
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            List of dictionary representations

        Example:
            users = [UserSerializer.from_model(u) for u in User.objects.all()]
            UserSerializer.dump_many(users)
        """
        cls._ensure_dump_ready()
        instances_list = instances if isinstance(instances, list) else list(instances)

        # FAST PATH: use msgspec native methods (significantly faster than Python iteration)
        if cls.__dump_fast_path__ and not exclude_none and not exclude_defaults and not exclude_unset and not by_alias:
            if cls.__orm_state_check_fields__:
                for instance in instances_list:
                    instance._ensure_dumpable_orm_state(cls.__orm_state_check_fields__)
            _astuple = msgspec_structs.astuple
            if cls.__tag_value__ is not None:
                keys = (cls.__tag_field__, *cls.__struct_encode_fields__)
                tag = cls.__tag_value__
                return [dict(zip(keys, (tag, *_astuple(instance)), strict=True)) for instance in instances_list]
            if cls.__has_rename__:
                keys = cls.__struct_encode_fields__
                return [dict(zip(keys, _astuple(instance), strict=True)) for instance in instances_list]
            _asdict = msgspec_structs.asdict
            return [_asdict(instance) for instance in instances_list]
        # _dump_impl gives the same dicts, so a mutant of this check changes only the cost.
        if not exclude_none and not exclude_defaults and not exclude_unset and not by_alias:  # pragma: no mutate
            return list(map(cls.__dump_function__, instances_list))

        # SLOW PATH: Need special handling (computed fields, write_only, exclude_*, etc.)
        field_specs = cls._get_dump_field_specs()
        computed_specs = cls._get_computed_dump_specs()
        return [
            instance._dump_impl(
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                by_alias=by_alias,
                field_specs=field_specs,
                computed_specs=computed_specs,
            )
            for instance in instances_list
        ]

    @classmethod
    def dump_many_json(
        cls: type[T],
        instances: Iterable[T],
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> bytes:
        """
        Serialize multiple instances to JSON bytes.

        Args:
            instances: Iterable of serializer instances to dump
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            JSON bytes representation of the list
        """
        data = cls.dump_many(
            instances,
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
        )
        return _json.encode(data)

    # -------------------------------------------------------------------------
    # Helper for getting value from source path
    # -------------------------------------------------------------------------


class SerializerView(Iterable[T]):
    """
    A view of a serializer with dynamic field selection.

    This class wraps a Serializer and applies field filtering during
    serialization. It does NOT create new serializer classes - it's
    a lightweight wrapper that filters fields at dump time.

    SerializerView is created by calling only(), exclude(), or use()
    on a Serializer class.

    Example:
        # These all return SerializerView instances:
        view = UserSerializer.only("id", "name")
        view = UserSerializer.exclude("password")
        view = UserSerializer.use("list")

        # Use the view to dump instances:
        view.dump(user)
        view.dump_many(users)

        # Or directly from model:
        view.from_model(user_instance)
    """

    __slots__ = ("_serializer_class", "_include_fields", "_exclude_fields", "_field_specs", "_computed_specs")

    def __init__(
        self,
        serializer_class: type[T],
        *,
        include_fields: frozenset[str] | None = None,
        exclude_fields: frozenset[str] | None = None,
    ) -> None:
        self._serializer_class = serializer_class
        self._include_fields = include_fields
        self._exclude_fields = exclude_fields
        self._field_specs = serializer_class._get_dump_field_specs(
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )
        self._computed_specs = serializer_class._get_computed_dump_specs(
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )

    def __iter__(self):
        """Allow iteration (returns empty iterator - use dump_many instead)."""
        return iter([])

    def only(self, *fields: str) -> SerializerView[T]:
        """Further restrict to only these fields."""
        new_include = frozenset(fields)
        if self._include_fields is not None:
            # Intersection with existing include
            new_include = self._include_fields & new_include
        return self._serializer_class._get_cached_view(
            include_fields=new_include,
            exclude_fields=self._exclude_fields,
        )

    def exclude(self, *fields: str) -> SerializerView[T]:
        """Exclude additional fields."""
        new_exclude = frozenset(fields)
        if self._exclude_fields is not None:
            new_exclude = self._exclude_fields | new_exclude
        return self._serializer_class._get_cached_view(
            include_fields=self._include_fields,
            exclude_fields=new_exclude,
        )

    def from_model(self, instance: Model, **kwargs) -> T:
        """Create a serializer instance from a Django model."""
        return self._serializer_class.from_model(instance, **kwargs)

    async def afrom_model(self, instance: Model, **kwargs) -> T:
        """Create a serializer instance from a Django model using async ORM loading."""
        return await self._serializer_class.afrom_model(instance, **kwargs)

    def dump(
        self,
        instance: T,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> dict[str, Any]:
        """
        Serialize an instance with field filtering applied.

        Args:
            instance: Serializer instance to dump
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            Filtered dictionary representation
        """
        return instance._dump_impl(
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
            include_fields=self._include_fields,
            exclude_fields=self._exclude_fields,
            field_specs=self._field_specs,
            computed_specs=self._computed_specs,
        )

    def dump_many(
        self,
        instances: Iterable[T],
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Serialize multiple instances with field filtering applied.

        Args:
            instances: Iterable of serializer instances
            exclude_none: If True, exclude fields with None values
            exclude_unset: If True, exclude fields that hold msgspec.UNSET
            exclude_defaults: If True, exclude fields with their default values
            by_alias: No effect. The alias of a field is its key in each output

        Returns:
            List of filtered dictionary representations
        """
        return [
            instance._dump_impl(
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                by_alias=by_alias,
                include_fields=self._include_fields,
                exclude_fields=self._exclude_fields,
                field_specs=self._field_specs,
                computed_specs=self._computed_specs,
            )
            for instance in instances
        ]

    def dump_json(
        self,
        instance: T,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> bytes:
        """Serialize an instance to JSON bytes with field filtering."""
        data = self.dump(
            instance,
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
        )
        return msgspec.json.encode(data)

    def dump_many_json(
        self,
        instances: Iterable[T],
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        by_alias: bool = False,
    ) -> bytes:
        """Serialize multiple instances to JSON bytes with field filtering."""
        data = self.dump_many(
            instances,
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            by_alias=by_alias,
        )
        return msgspec.json.encode(data)
