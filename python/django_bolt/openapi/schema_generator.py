from __future__ import annotations

import enum
import http.client
import inspect
import re
from collections.abc import Callable
from dataclasses import replace
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

import msgspec

from ..datastructures import UploadFile
from ..responses import (
    HTML,
    EventSourceResponse,
    File,
    FileResponse,
    PlainText,
    Redirect,
    StreamingResponse,
)
from ..serializers.fields import _FieldMarker
from ..typing import is_msgspec_struct, is_optional, unwrap_optional
from ..views import _layer
from .spec import (
    Example,
    OpenAPI,
    OpenAPIHeader,
    OpenAPIMediaType,
    OpenAPIResponse,
    Operation,
    Parameter,
    PathItem,
    Reference,
    RequestBody,
    Schema,
    SecurityScheme,
    Tag,
)

if TYPE_CHECKING:
    from ..api import BoltAPI
    from .config import OpenAPIConfig

__all__ = ("ComponentNameCollisionError", "OpenAPIStrictError", "SchemaGenerator")


class OpenAPIStrictError(ValueError):
    """``OpenAPIConfig(strict=True)``: the schema would contain shapes codegen cannot type.

    Raised by ``SchemaGenerator.generate`` with every offender listed at once:
    routes whose success response is an opaque ``{"type": "object"}`` (no return
    annotation / ``response_model``) and components that needed the
    ``module.qualname`` fallback because two types share a short name.
    """


# ``response_class`` → (media type, body schema). ``Redirect`` is handled
# separately (3xx, no body). Order matters: subclasses before their bases.
_RESPONSE_CLASS_MEDIA: tuple[tuple[type, str, Schema], ...] = (
    (HTML, "text/html", Schema(type="string")),
    (PlainText, "text/plain", Schema(type="string")),
    (EventSourceResponse, "text/event-stream", Schema(type="string")),
    (StreamingResponse, "application/octet-stream", Schema(type="string", format="binary")),
    (File, "application/octet-stream", Schema(type="string", format="binary")),
    (FileResponse, "application/octet-stream", Schema(type="string", format="binary")),
)


def _response_class_media(response_class: type | None) -> tuple[str, Schema] | None:
    if response_class is None:
        return None
    for cls, media_type, schema in _RESPONSE_CLASS_MEDIA:
        if issubclass(response_class, cls):
            return media_type, schema
    return None


def _struct_origin(annotation: Any) -> type | None:
    """The msgspec.Struct class behind ``annotation`` — itself, or the origin of ``Struct[T]``."""
    if is_msgspec_struct(annotation):
        return annotation
    origin = get_origin(annotation)
    return origin if is_msgspec_struct(origin) else None


def _type_display_name(annotation: Any) -> str:
    """``Page[UserRead]`` for a parametrized struct, ``__name__`` otherwise (msgspec's title)."""
    origin = get_origin(annotation)
    if origin is not None and get_args(annotation):
        args = ", ".join(_type_display_name(a) for a in get_args(annotation))
        return f"{getattr(origin, '__name__', repr(origin))}[{args}]"
    return getattr(annotation, "__name__", None) or repr(annotation)


class ComponentNameCollisionError(ValueError):
    """Two distinct types claim the same OpenAPI component name.

    Structs and enums share a single ``#/components/schemas`` namespace keyed by
    ``__name__``. When two different types resolve to the same name, emitting a
    shared ``$ref`` would silently point one of them at the wrong schema, so the
    generator raises this instead. The message is built in ``__init__`` (rather
    than at the raise site) so the colliding types stay introspectable and the
    raise site stays terse.
    """

    def __init__(self, schema_name: str, new_type: type, existing_type: type) -> None:
        self.schema_name = schema_name
        self.new_type = new_type
        self.existing_type = existing_type
        super().__init__(
            f"OpenAPI component name collision: {new_type!r} and {existing_type!r} "
            f"both map to component schema '{schema_name}'. Rename one type so "
            f"each component has a unique name."
        )


# Mapping from auth backend scheme_name to OpenAPI security scheme identifier
_SCHEME_NAME_MAP: dict[str, str] = {
    "jwt": "BearerAuth",
    "api_key": "ApiKeyAuth",
}


_PATH_PARAM_RE = re.compile(r"{([^{}]+)}")


def _extract_path_param_names(path: str) -> list[str]:
    """Return path parameter names declared in a route path (e.g. {pk} → "pk")."""
    return _PATH_PARAM_RE.findall(path)


def _is_tagged_struct_union(arms: list[Any]) -> bool:
    """True when every union arm is a tagged ``msgspec.Struct`` subclass.

    Accepts both raw classes (``typing.Union`` / PEP 604) and
    ``msgspec.inspect.StructType`` wrappers (the inspect-path branch).
    A "tagged" Struct is one whose ``__struct_config__.tag`` is set
    (msgspec resolves ``tag=True`` to the class name at class creation),
    which is the only case where Swagger UI's ``oneOf`` discriminator
    rendering buys us anything over ``anyOf``.
    """
    if not arms:
        return False
    for arm in arms:
        # msgspec.inspect.StructType wraps the actual struct class on .cls
        cls = getattr(arm, "cls", arm)
        if not isinstance(cls, type) or not issubclass(cls, msgspec.Struct):
            return False
        struct_config = getattr(cls, "__struct_config__", None)
        if struct_config is None or getattr(struct_config, "tag", None) is None:
            return False
    return True


# Placeholder values used when synthesising response examples per Struct field.
# Picked to match what Swagger UI would render itself for unspecified examples,
# so users see consistent ``"string"``/``0`` placeholders across all arms.
_PLACEHOLDER_BY_MSGSPEC_TYPE: dict[str, Any] = {
    "IntType": 0,
    "FloatType": 0.0,
    "StrType": "string",
    "BoolType": True,
    "BytesType": "",
    "DateTimeType": "2024-01-01T00:00:00Z",
    "DateType": "2024-01-01",
    "TimeType": "00:00:00",
    "UUIDType": "00000000-0000-0000-0000-000000000000",
    "DecimalType": "0",
    "NoneType": None,
}


def _synthesize_example(field_type: Any, depth: int = 0) -> Any:
    """Build a Swagger-style placeholder value for a ``msgspec.inspect.*Type``.

    Used only for OpenAPI ``examples:`` rendering. Bounded recursion depth
    prevents infinite loops on self-referential Structs.
    """
    if depth > 5:
        return None
    type_name = type(field_type).__name__
    placeholder = _PLACEHOLDER_BY_MSGSPEC_TYPE.get(type_name)
    if placeholder is not None or type_name == "NoneType":
        return placeholder
    if type_name == "StructType":
        return _synthesize_struct_example(field_type.cls, depth + 1)
    if type_name == "ListType":
        item_type = getattr(field_type, "item_type", None)
        return [_synthesize_example(item_type, depth + 1)] if item_type is not None else []
    if type_name == "DictType":
        return {}
    if type_name == "UnionType":
        for arm in field_type.types:
            if type(arm).__name__ != "NoneType":
                return _synthesize_example(arm, depth + 1)
        return None
    if type_name == "LiteralType":
        values = getattr(field_type, "values", None)
        return values[0] if values else None
    if type_name == "EnumType":
        cls = getattr(field_type, "cls", None)
        members = list(cls) if cls is not None else []
        return members[0].value if members else None
    return None


def _synthesize_struct_example(struct_cls: type, depth: int = 0) -> dict[str, Any]:
    """Build a placeholder dict for a tagged ``msgspec.Struct`` subclass.

    Includes the resolved tag field so each example clearly identifies its
    variant — that's the whole point of emitting per-arm examples.
    """
    struct_info = msgspec.inspect.type_info(struct_cls)
    value: dict[str, Any] = {}
    for field in struct_info.fields:
        value[field.encode_name] = _synthesize_example(field.type, depth + 1)
    tag_field = getattr(struct_info, "tag_field", None)
    tag = getattr(struct_info, "tag", None)
    if tag_field and tag is not None:
        value[tag_field] = tag
    return value


def _build_union_examples(response_type: Any) -> dict[str, Example] | None:
    """Emit per-arm examples for a tagged Struct union *single-object* response.

    Returns a mapping ``{tag → Example}`` that Swagger UI renders as an
    example-picker dropdown — one example per branch of the tagged union.
    Only fires for the single-object shape ``Union[A, B, ...]``.

    Explicitly skips ``list[Union[A, B, ...]]`` because a runtime list can
    contain a mix of arms; collapsing it to per-arm dropdowns of
    homogeneous lists ("here are 10 Cats" / "here are 10 Dogs")
    misrepresents the schema. Swagger's default rendering of
    ``items.oneOf`` already produces a heterogeneous example array (one
    of each variant intermixed), which is the truthful representation.

    Returns ``None`` for anything else (untagged unions, primitives,
    nested unions, etc.) so Swagger's default rendering takes over.
    """
    if response_type is None:
        return None
    origin = get_origin(response_type)
    if origin is Annotated:
        response_type = get_args(response_type)[0]
        origin = get_origin(response_type)
    if origin not in (Union, UnionType):
        return None
    arms = [a for a in get_args(response_type) if a is not type(None)]
    if not _is_tagged_struct_union(arms):
        return None
    examples: dict[str, Example] = {}
    for arm in arms:
        struct_info = msgspec.inspect.type_info(arm)
        tag = getattr(struct_info, "tag", None)
        if tag is None:
            continue
        examples[str(tag)] = Example(
            summary=f"{arm.__name__} variant",
            value=_synthesize_struct_example(arm),
        )
    return examples or None


class SchemaGenerator:
    """Generate OpenAPI schema from BoltAPI routes."""

    def __init__(self, api: BoltAPI, config: OpenAPIConfig) -> None:
        """Initialize schema generator.

        Args:
            api: BoltAPI instance to generate schema for.
            config: OpenAPI configuration.
        """
        self.api = api
        self.config = config
        self.schemas: dict[str, Schema] = {}  # Component schemas registry, keyed by final name
        # Component naming is two-pass (matching `msgspec.json.schema_components`):
        # during route processing each struct/enum is registered by *type identity*
        # with a shared, not-yet-named `Reference`; once every component is known,
        # `_finalize_component_names` assigns names — short `__name__` normally,
        # `module.qualname` only for the types whose short names actually collide —
        # and stamps each shared Reference in place. Keying by type (not name)
        # means two same-named types from different modules coexist instead of one
        # silently stealing the other's `$ref`. Dict insertion order is the
        # registration order the name map iterates.
        self._component_ref: dict[type, Reference] = {}
        self._component_schema: dict[type, Schema] = {}
        # strict mode bookkeeping (see OpenAPIStrictError)
        self._opaque_operations: list[str] = []
        self._qualified_components: list[str] = []

    @staticmethod
    def _schema_kwargs(**kwargs: Any) -> dict[str, Any]:
        """Keep only meaningful schema kwargs so unconstrained fields stay unset."""
        return {key: value for key, value in kwargs.items() if value is not None}

    @staticmethod
    def _with_default(schema: Schema | Reference, default: Any) -> Schema | Reference:
        """Attach a default value to either an inline schema or a component reference."""
        if isinstance(schema, Schema):
            return replace(schema, default=default)
        return Schema(all_of=[schema], default=default)

    @staticmethod
    def _enum_values_schema(values: list[Any] | tuple[Any, ...]) -> Schema:
        """Infer the narrowest enum schema that fits the provided values."""
        enum_values = list(values)
        if all(isinstance(v, str) for v in enum_values):
            return Schema(type="string", enum=enum_values)
        if all(isinstance(v, int) for v in enum_values):
            return Schema(type="integer", enum=enum_values)
        return Schema(enum=enum_values)

    @staticmethod
    def _own_docstring(cls: type) -> str | None:
        """Return a class's *own* cleaned docstring, or None when it has none.

        Reads ``__dict__`` directly rather than ``inspect.getdoc`` so a class
        that defines no docstring doesn't inherit its base's (e.g. an
        undocumented Struct would otherwise pick up ``msgspec.Struct``'s
        multi-page base docstring). ``cleandoc`` strips uniform indentation so
        multi-line docstrings render correctly as JSDoc. Matches what
        ``msgspec.json.schema_components`` carries through.
        """
        own_doc = cls.__dict__.get("__doc__")
        return inspect.cleandoc(own_doc) if own_doc else None

    def _enum_schema(self, enum_cls: type, *, register_component: bool) -> Schema | Reference:
        """Promote a named enum to a component (``$ref``) or inline its values.

        Single source for the "promote in body/response contexts, inline in
        query/param contexts" policy shared by the msgspec-inspect enum branch
        and the bare enum-class branch of ``_type_to_schema`` — so the two
        paths can't drift (a missing bare-enum path was exactly bug #246's gap).
        """
        if register_component:
            return self._enum_to_component_schema(enum_cls)
        return self._enum_values_schema([e.value for e in enum_cls])

    def _union_schema(
        self,
        inner_schemas: list[Schema | Reference],
        *,
        has_none: bool,
        tagged: bool,
    ) -> Schema | Reference:
        """Assemble an OpenAPI 3.1 union schema from already-built arm schemas.

        Centralizes the null-encoding + collapse policy shared by the two union
        branches (msgspec-inspect ``UnionType`` and typing ``Union``/PEP 604):

        - ``has_none`` appends a ``{"type": "null"}`` arm — OpenAPI 3.1 expresses
          nullability via ``null`` in the type union, not the legacy 3.0
          ``nullable: true``. Preserving it keeps generated specs round-tripping
          through tooling like openapi-typescript (which would otherwise drop
          the ``| null`` arm).
        - A single remaining arm collapses to itself (no needless wrapper).
        - ``tagged`` selects ``one_of`` (tagged Struct unions → Swagger UI
          per-variant dropdown + 3.1 discriminator semantics) over ``any_of``.
        """
        arms = list(inner_schemas)
        if has_none:
            arms.append(Schema(type="null"))
        if len(arms) == 1:
            return arms[0]
        if tagged:
            return Schema(one_of=arms)
        return Schema(any_of=arms)

    @staticmethod
    def _mapping_schema(value_schema: Schema | Reference | None) -> Schema:
        """``object`` schema for a ``dict[K, V]``/mapping.

        A typed value emits ``additionalProperties: <schema for V>`` (mirroring
        the list item handling and matching ``msgspec.json.schema``); an untyped
        value (bare ``dict`` / ``dict[str, Any]``) keeps
        ``additionalProperties: true`` rather than regressing to a bare
        ``{"type": "object"}``. JSON object keys are always strings, so only V
        is described.
        """
        if value_schema is None:
            return Schema(type="object", additional_properties=True)
        return Schema(type="object", additional_properties=value_schema)

    def _summary_and_description(
        self, handler: Any, summary: str | None, description: str | None
    ) -> tuple[str | None, str | None]:
        """Fill any missing summary/description from the handler's docstring.

        First line → summary, remainder → description, honoring
        ``config.use_handler_docstrings``. Explicit metadata already on
        ``summary``/``description`` is left untouched. Shared by the HTTP and
        WebSocket operation builders.
        """
        if (summary is None or description is None) and self.config.use_handler_docstrings and handler.__doc__:
            doc = inspect.cleandoc(handler.__doc__)
            lines = doc.split("\n", 1)
            if summary is None:
                summary = lines[0]
            if description is None and len(lines) > 1:
                description = lines[1].strip()
        return summary, description

    def _numeric_type_schema(self, type_annotation: Any, schema_type: str) -> Schema:
        """Build a numeric schema from msgspec numeric type metadata."""
        return Schema(
            **self._schema_kwargs(
                type=schema_type,
                minimum=type_annotation.ge,
                exclusive_minimum=type_annotation.gt,
                maximum=type_annotation.le,
                exclusive_maximum=type_annotation.lt,
                multiple_of=type_annotation.multiple_of,
            )
        )

    @staticmethod
    def _apply_json_schema_extra(schema: Schema, extra: dict[str, Any]) -> Schema:
        """Merge a msgspec ``extra_json_schema`` mapping onto a Schema in place.

        ``msgspec.inspect`` collects the *informational* JSON-schema fields of a
        ``Meta(...)`` annotation — ``title``/``description``/``examples`` plus any
        explicit ``extra_json_schema`` — into this dict, keyed by their
        JSON-schema names (the numeric/string *constraints* live on the wrapped
        ``*Type`` instead). Map each key onto the matching Schema dataclass
        attribute, translating camelCase JSON keys (e.g. ``maxLength``) to the
        snake_case field name via the alias map. Keys with no corresponding
        Schema field (e.g. arbitrary ``x-`` extensions) are skipped — the spec
        dataclass cannot represent them. Returns the same Schema for chaining.
        """
        alias_map = Schema.field_aliases()  # {json_alias: field_name}
        for key, value in extra.items():
            attr = alias_map.get(key, key)
            if hasattr(schema, attr):
                setattr(schema, attr, value)
        return schema

    def _msgspec_field_schema(
        self, field: Any, *, register_component: bool = False
    ) -> tuple[str, Schema | Reference, bool]:
        """Build a schema and required flag for a msgspec-inspected field."""
        field_name = field.encode_name
        field_schema = self._type_to_schema(field.type, register_component=register_component)

        default = field.default
        has_default = default is not msgspec.NODEFAULT
        field_required = field.required

        # Unwrap Serializer field() markers: msgspec stores the _FieldMarker as
        # the default, so field.required is False even when the marker carries
        # only config and no real default.
        if isinstance(default, _FieldMarker):
            if default.config.has_default():
                default = default.config.get_default()
            else:
                has_default = False
                field_required = True

        if has_default:
            field_schema = self._with_default(field_schema, default)
            field_required = False
        elif field.default_factory in (list, dict, set, bytearray):
            # Mutable-default fields carry their default on ``field.default_factory``
            # instead of ``field.default`` — materialize it so the schema gains
            # ``default: []``/``{}``. The whitelist mirrors ``msgspec.json.schema``
            # exactly (``_json_schema.py``): only these four builtin factories are
            # materialized. Immutable factories (``tuple``/``frozenset``) are stored
            # by msgspec as a plain ``field.default`` and so are handled by the
            # ``has_default`` branch above; arbitrary factories (``datetime.now``,
            # a ``lambda``, a class) deliberately get NO default — both because
            # msgspec emits none and because calling them here could embed a
            # non-JSON-encodable object that crashes spec serialization. The
            # _FieldMarker path above never reaches here (markers live on
            # ``field.default``, leaving ``default_factory`` as NODEFAULT).
            field_schema = self._with_default(field_schema, field.default_factory())
            field_required = False

        return field_name, field_schema, field_required

    def generate(self) -> OpenAPI:
        """Generate complete OpenAPI schema.

        Returns:
            OpenAPI schema object.
        """
        openapi = self.config.to_openapi_schema()

        # Track auth schemes seen during _extract_security calls
        self._seen_schemes: set[str] = set()
        self._api_key_header: str | None = None

        # Generate path items from routes and collect tags
        paths: dict[str, PathItem] = {}
        collected_tags: set[str] = set()

        # The QUERY method is only representable as a Path Item operation in
        # OpenAPI 3.2.0+. Emitting a `query` operation under the default 3.1.0
        # declaration produces a document that fails strict 3.1 validation
        # ("unevaluatedProperties: false"). Bump the declared version only when
        # a QUERY route is actually present, so non-QUERY APIs keep declaring
        # 3.1.0 for maximum downstream tooling compatibility.
        has_query_operation = False

        # Process HTTP routes
        for method, path, handler_id, handler in self.api._routes:
            # Skip OpenAPI docs routes (always excluded)
            if path.startswith(self.config.path):
                continue

            # Skip paths based on exclude_paths configuration
            should_exclude = False
            for exclude_prefix in self.config.exclude_paths:
                if path.startswith(exclude_prefix):
                    should_exclude = True
                    break

            if should_exclude:
                continue

            # Get handler metadata
            meta = self.api._handler_meta.get(handler_id, {})
            if _layer(meta.get("include_in_schema"), self.api.include_in_schema) is False:
                continue

            if path not in paths:
                paths[path] = PathItem()

            # Create operation
            operation = self._create_operation(
                handler=handler,
                method=method,
                path=path,
                meta=meta,
                handler_id=handler_id,
            )

            # Collect tags from operation
            if operation.tags:
                collected_tags.update(operation.tags)

            # Add operation to path item
            method_lower = method.lower()
            if method_lower == "query":
                has_query_operation = True
            setattr(paths[path], method_lower, operation)

        # Process WebSocket routes
        for ws_path, handler_id, handler in self.api._websocket_routes:
            # Skip OpenAPI docs routes (always excluded)
            if ws_path.startswith(self.config.path):
                continue

            # Skip paths based on exclude_paths configuration
            should_exclude = False
            for exclude_prefix in self.config.exclude_paths:
                if ws_path.startswith(exclude_prefix):
                    should_exclude = True
                    break

            if should_exclude:
                continue

            if ws_path not in paths:
                paths[ws_path] = PathItem()

            # Get handler metadata
            meta = self.api._handler_meta.get(handler_id, {})

            # Create WebSocket operation (as GET with upgrade)
            operation = self._create_websocket_operation(
                handler=handler,
                path=ws_path,
                meta=meta,
                handler_id=handler_id,
            )

            # Collect tags from operation
            if operation.tags:
                collected_tags.update(operation.tags)

            # Mark path item as WebSocket and add GET operation
            # WebSockets start with HTTP upgrade from GET request
            paths[ws_path].get = operation

            # Add x-websocket extension to mark this as a WebSocket endpoint
            if paths[ws_path].extensions is None:
                paths[ws_path].extensions = {}
            paths[ws_path].extensions["x-websocket"] = True

        openapi.paths = paths

        if has_query_operation:
            openapi.openapi = "3.2.0"

        # Auto-register security schemes from auth backends used on routes
        self._register_security_schemes(openapi)

        # Assign final component names now that every component type is known,
        # then expose the populated registry. Names are decided before this point
        # only as type-keyed placeholders, so this is what actually fills
        # self.schemas and stamps the shared `$ref`s.
        self._finalize_component_names()
        if self.schemas:
            openapi.components.schemas = self.schemas

        # Collect and merge tags
        openapi.tags = self._collect_tags(collected_tags)

        if self.config.strict and (self._opaque_operations or self._qualified_components):
            problems = []
            if self._opaque_operations:
                problems.append(
                    "routes with an untyped JSON response (add a return annotation or response_model): "
                    + ", ".join(self._opaque_operations)
                )
            if self._qualified_components:
                problems.append(
                    "component names that fell back to module.qualname because two types share a "
                    "short name (rename one): " + ", ".join(self._qualified_components)
                )
            raise OpenAPIStrictError("OpenAPI strict mode: " + "; ".join(problems))

        return openapi

    def _create_operation(
        self,
        handler: Any,
        method: str,
        path: str,
        meta: dict[str, Any],
        handler_id: int,
    ) -> Operation:
        """Create OpenAPI Operation for a route handler.

        Args:
            handler: Handler function.
            method: HTTP method.
            path: Route path.
            meta: Handler metadata from BoltAPI.
            handler_id: Handler ID.

        Returns:
            Operation object.
        """
        # Prefer explicit metadata over docstring extraction
        summary = meta.get("openapi_summary")
        description = meta.get("openapi_description")
        summary, description = self._summary_and_description(handler, summary, description)

        # Extract parameters
        parameters = self._extract_parameters(meta, path)

        # Extract request body
        request_body = self._extract_request_body(meta)

        # Extract responses (pass handler_id for auth error responses)
        responses = self._extract_responses(meta, handler_id, method=method, path=path)

        # Extract security requirements
        security = self._extract_security(handler_id)

        # Prefer explicit tags over auto-extracted tags
        tags = meta.get("openapi_tags")
        if tags is None:
            # Fallback to auto-extraction from handler module or class name
            tags = self._extract_tags(handler)

        operation = Operation(
            summary=summary,
            description=description,
            parameters=parameters or None,
            request_body=request_body,
            responses=responses,
            security=security,
            tags=tags,
            operation_id=f"{method.lower()}_{handler.__name__}",
        )

        return operation

    def _create_websocket_operation(
        self,
        handler: Any,
        path: str,
        meta: dict[str, Any],
        handler_id: int,
    ) -> Operation:
        """Create OpenAPI Operation for a WebSocket handler.

        WebSocket connections start as HTTP GET requests with an Upgrade header.
        This method creates an OpenAPI operation that documents the WebSocket endpoint.

        Args:
            handler: Handler function.
            path: Route path.
            meta: Handler metadata from BoltAPI.
            handler_id: Handler ID.

        Returns:
            Operation object for WebSocket endpoint.
        """
        # Prefer explicit metadata over docstring extraction
        summary = meta.get("openapi_summary")
        description = meta.get("openapi_description")
        summary, description = self._summary_and_description(handler, summary, description)

        # Add WebSocket indicator to summary/description
        if summary and not summary.lower().startswith("websocket"):
            summary = f"WebSocket: {summary}"
        elif not summary:
            summary = "WebSocket Connection"

        if description:
            description = (
                f"**WebSocket Endpoint**\n\n{description}\n\n"
                "This endpoint establishes a WebSocket connection. Use `ws://` or `wss://` protocol."
            )
        else:
            description = (
                "**WebSocket Endpoint**\n\n"
                "Establishes a WebSocket connection for real-time bidirectional communication.\n\n"
                "Use `ws://` or `wss://` protocol to connect."
            )

        # Extract parameters (path params, query params, headers, cookies)
        # Skip body/form/file parameters as WebSocket doesn't use request body
        parameters = self._extract_parameters(meta, path)

        # Add required WebSocket upgrade headers as parameters
        upgrade_headers = [
            Parameter(
                name="Upgrade",
                param_in="header",
                required=True,
                schema=Schema(type="string", enum=["websocket"]),
                description="Must be 'websocket' to upgrade the connection",
            ),
            Parameter(
                name="Connection",
                param_in="header",
                required=True,
                schema=Schema(type="string", enum=["Upgrade"]),
                description="Must be 'Upgrade' to upgrade the connection",
            ),
        ]
        parameters.extend(upgrade_headers)

        # WebSocket endpoints don't have traditional HTTP responses
        # Document the 101 Switching Protocols response.
        # Per OpenAPI 3.1 the Header Object MUST NOT specify `name` or
        # `in` — both are derived from the `headers` map key + the
        # implicit `header` location — so use `OpenAPIHeader` (which
        # excludes those fields on serialization) rather than
        # `Parameter`. Validators reject the latter.
        responses = {
            "101": OpenAPIResponse(
                description="Switching Protocols - WebSocket connection established",
                headers={
                    "Upgrade": OpenAPIHeader(
                        schema=Schema(type="string", enum=["websocket"]),
                    ),
                    "Connection": OpenAPIHeader(
                        schema=Schema(type="string", enum=["Upgrade"]),
                    ),
                },
            ),
            "400": OpenAPIResponse(
                description="Bad Request - Invalid WebSocket upgrade request",
            ),
            "403": OpenAPIResponse(
                description="Forbidden - Authentication or authorization failed",
            ),
        }

        # Extract security requirements
        security = self._extract_security(handler_id)

        # Prefer explicit tags over auto-extracted tags
        tags = meta.get("openapi_tags")
        if tags is None:
            # Fallback to auto-extraction from handler module or class name
            tags = self._extract_tags(handler)

        # Add "WebSocket" tag if not present
        if tags:
            if "WebSocket" not in tags and "Websocket" not in tags and "websocket" not in tags:
                tags = ["WebSocket"] + tags
        else:
            tags = ["WebSocket"]

        operation = Operation(
            summary=summary,
            description=description,
            parameters=parameters or None,
            request_body=None,  # WebSocket doesn't use HTTP request body
            responses=responses,
            security=security,
            tags=tags,
            operation_id=f"websocket_{handler.__name__}",
        )

        return operation

    def _extract_parameters(self, meta: dict[str, Any], path: str) -> list[Parameter]:
        """Extract OpenAPI parameters from handler metadata.

        Args:
            meta: Handler metadata.
            path: Route path.

        Returns:
            List of Parameter objects.
        """
        parameters: list[Parameter] = []
        fields = meta.get("fields", [])

        for field in fields:
            # Access FieldDefinition attributes directly
            source = field.source
            name = field.name
            alias = field.alias or name
            annotation = field.annotation
            default = field.default

            # Skip request, body, form, file, and dependency parameters
            if source in ("request", "body", "form", "file", "dependency"):
                continue

            # Map source to OpenAPI parameter location
            param_in = {
                "path": "path",
                "query": "query",
                "header": "header",
                "cookie": "cookie",
            }.get(source)

            if not param_in:
                continue

            # Determine if required
            required = (
                param_in == "path"  # Path params always required
                or (default == inspect.Parameter.empty and not is_optional(annotation))
            )

            # Handle msgspec.Struct in query parameters
            if source == "query" and is_msgspec_struct(annotation):
                struct_info = msgspec.inspect.type_info(annotation)
                for struct_field in struct_info.fields:
                    field_name, field_schema, field_required = self._msgspec_field_schema(struct_field)
                    parameters.append(
                        Parameter(
                            name=field_name,
                            param_in="query",
                            required=field_required,
                            schema=field_schema,
                            description=f"Parameter {field_name}",
                        )
                    )
                continue

            # Get schema for parameter type
            schema = self._type_to_schema(annotation)
            if default not in (inspect.Parameter.empty, None):
                schema = replace(schema, default=default)

            parameter = Parameter(
                name=alias,
                param_in=param_in,
                required=required,
                schema=schema,
                description=f"Parameter {alias}",
            )
            parameters.append(parameter)

        # Every path parameter declared in the route URL must appear in the
        # OpenAPI spec, even when the handler resolves it indirectly (e.g.
        # ViewSet mixins read {pk} from self.request.params instead of binding
        # it as a function argument). Handlers that declare the parameter
        # explicitly already produced a typed entry above; the URL-declared
        # ones fill in the rest with the OpenAPI-default string type.
        bound_path_names = {p.name for p in parameters if p.param_in == "path"}
        for param_name in _extract_path_param_names(path):
            if param_name in bound_path_names:
                continue
            parameters.append(
                Parameter(
                    name=param_name,
                    param_in="path",
                    required=True,
                    schema=Schema(type="string"),
                    description=f"Parameter {param_name}",
                )
            )
            bound_path_names.add(param_name)

        return parameters

    def _extract_request_body(self, meta: dict[str, Any]) -> RequestBody | None:
        """Extract OpenAPI RequestBody from handler metadata.

        Args:
            meta: Handler metadata.

        Returns:
            RequestBody object or None.
        """
        body_param = meta.get("body_struct_param")
        body_type = meta.get("body_struct_type")

        if not body_param or not body_type:
            # Check for form/file fields
            fields = meta.get("fields", [])
            form_fields = [f for f in fields if f.source in ("form", "file")]

            if form_fields:
                # Multipart form data
                properties: dict[str, Schema | Reference] = {}
                required: list[str] = []
                for field in form_fields:
                    name = field.alias or field.name
                    annotation = field.annotation
                    default = field.default

                    # Form fields annotated with a Struct/Serializer are flattened:
                    # the runtime form extractor reads each struct field as a
                    # top-level form key, so the schema must mirror that shape.
                    # Use msgspec.structs.fields (raw Python types) rather than
                    # msgspec.inspect.type_info (CustomType-wrapped) so the
                    # UploadFile branch in _type_to_schema fires for file fields.
                    unwrapped = unwrap_optional(annotation)
                    if is_msgspec_struct(unwrapped):
                        for struct_field in msgspec.structs.fields(unwrapped):
                            sub_name, sub_schema, sub_required = self._msgspec_field_schema(
                                struct_field, register_component=False
                            )
                            properties[sub_name] = sub_schema
                            if sub_required:
                                required.append(sub_name)
                        continue

                    properties[name] = self._type_to_schema(annotation)
                    if default == inspect.Parameter.empty and not is_optional(annotation):
                        required.append(name)

                schema = Schema(
                    type="object",
                    properties=properties,
                    required=required or None,
                )

                return RequestBody(
                    description="Form data",
                    content={
                        "multipart/form-data": OpenAPIMediaType(schema=schema),
                        "application/x-www-form-urlencoded": OpenAPIMediaType(schema=schema),
                    },
                    required=bool(required),
                )

            return None

        # JSON request body
        schema = self._type_to_schema(body_type, register_component=True)

        return RequestBody(
            description=f"Request body for {body_param}",
            content={
                "application/json": OpenAPIMediaType(schema=schema),
            },
            required=True,
        )

    def _extract_responses(
        self, meta: dict[str, Any], handler_id: int, *, method: str = "", path: str = ""
    ) -> dict[str, OpenAPIResponse]:
        """Extract OpenAPI responses from handler metadata.

        Args:
            meta: Handler metadata.
            handler_id: Handler ID for checking authentication requirements.

        Returns:
            Dictionary mapping status codes to Response objects.
        """
        responses: dict[str, OpenAPIResponse] = {}

        if meta.get("is_multi_response"):
            # Multi-response mode: per-status-code response schemas
            response_map = meta["response_map"]
            for code in sorted(c for c in response_map if isinstance(c, int)):
                resp_type = response_map[code]
                desc = http.client.responses.get(code, f"Response {code}")
                if resp_type is None:
                    responses[str(code)] = OpenAPIResponse(description=desc)
                else:
                    schema = self._type_to_schema(resp_type, register_component=True)
                    responses[str(code)] = OpenAPIResponse(
                        description=desc,
                        content={
                            "application/json": OpenAPIMediaType(
                                schema=schema,
                                examples=_build_union_examples(resp_type),
                            )
                        },
                    )
            # Ellipsis catch-all → OpenAPI "default" response
            if ... in response_map:
                ellipsis_type = response_map[...]
                if ellipsis_type is None:
                    responses["default"] = OpenAPIResponse(description="Default response")
                else:
                    schema = self._type_to_schema(ellipsis_type, register_component=True)
                    responses["default"] = OpenAPIResponse(
                        description="Default response",
                        content={
                            "application/json": OpenAPIMediaType(
                                schema=schema,
                                examples=_build_union_examples(ellipsis_type),
                            )
                        },
                    )
            # fall through to error response logic below
        else:
            # Single-response mode (existing behavior, unchanged)
            # Get response type
            response_type = meta.get("response_type")
            default_status = meta.get("default_status_code", 200)

            response_class = meta.get("response_class")
            has_type = bool(response_type) and response_type != inspect._empty
            media = _response_class_media(response_class)

            if response_class is not None and issubclass(response_class, Redirect):
                # A redirect has no body; the default 200 is meaningless here.
                status = default_status if default_status != 200 else 307
                responses[str(status)] = OpenAPIResponse(
                    description=http.client.responses.get(status, "Redirect"),
                    headers={"Location": OpenAPIHeader(schema=Schema(type="string"))},
                )
            elif media is not None:
                media_type, body_schema = media
                if has_type:
                    body_schema = self._type_to_schema(response_type, register_component=True)
                responses[str(default_status)] = OpenAPIResponse(
                    description="Successful response",
                    content={media_type: OpenAPIMediaType(schema=body_schema)},
                )
            elif has_type:
                schema = self._type_to_schema(response_type, register_component=True)

                responses[str(default_status)] = OpenAPIResponse(
                    description="Successful response",
                    content={
                        "application/json": OpenAPIMediaType(
                            schema=schema,
                            examples=_build_union_examples(response_type),
                        ),
                    },
                )
            else:
                # Opaque JSON: nothing to type. Recorded for strict mode.
                self._opaque_operations.append(f"{method} {path}")
                responses[str(default_status)] = OpenAPIResponse(
                    description="Successful response",
                    content={
                        "application/json": OpenAPIMediaType(schema=Schema(type="object")),
                    },
                )

        # Add common error responses if enabled in config
        if self.config.include_error_responses:
            # Check if request body is present (for 422 validation errors)
            has_request_body = meta.get("body_struct_param") or any(
                f.source in ("body", "form", "file") for f in meta.get("fields", [])
            )

            if has_request_body:
                # 422 Unprocessable Entity - validation errors
                responses["422"] = OpenAPIResponse(
                    description="Validation Error - Request data failed validation",
                    content={
                        "application/json": OpenAPIMediaType(schema=self._get_validation_error_schema()),
                    },
                )

        return responses

    def _get_validation_error_schema(self) -> Schema:
        """Get schema for 422 validation error responses.

        FastAPI-compatible format: {"detail": [array of validation errors]}

        Returns:
            Schema for validation errors matching FastAPI format.
        """
        return Schema(
            type="object",
            properties={
                "detail": Schema(
                    type="array",
                    description="List of validation errors",
                    items=Schema(
                        type="object",
                        properties={
                            "type": Schema(
                                type="string",
                                description="Error type",
                                example="validation_error",
                            ),
                            "loc": Schema(
                                type="array",
                                description="Location of the error (field path)",
                                items=Schema(
                                    one_of=[
                                        Schema(type="string"),
                                        Schema(type="integer"),
                                    ]
                                ),
                                example=["body", "is_active"],
                            ),
                            "msg": Schema(
                                type="string",
                                description="Error message",
                                example="Expected `bool`, got `int`",
                            ),
                            "input": Schema(
                                description="The input value that caused the error (optional)",
                            ),
                        },
                        required=["type", "loc", "msg"],
                    ),
                ),
            },
            required=["detail"],
        )

    def _register_security_schemes(self, openapi: OpenAPI) -> None:
        """Auto-register SecurityScheme definitions from auth backends collected during generation.

        Uses backend info accumulated by _extract_security calls to register
        the corresponding SecurityScheme in components.security_schemes,
        preserving any user-defined schemes.
        """
        if not self._seen_schemes:
            return

        existing = openapi.components.security_schemes or {}
        needs_jwt = "jwt" in self._seen_schemes and "BearerAuth" not in existing
        needs_api_key = "api_key" in self._seen_schemes and "ApiKeyAuth" not in existing

        if not needs_jwt and not needs_api_key:
            return

        schemes = dict(existing)

        if needs_jwt:
            schemes["BearerAuth"] = SecurityScheme(
                type="http",
                scheme="bearer",
                bearer_format="JWT",
            )

        if needs_api_key:
            schemes["ApiKeyAuth"] = SecurityScheme(
                type="apiKey",
                name=self._api_key_header,
                security_scheme_in="header",
            )

        openapi.components.security_schemes = schemes

    def _extract_security(self, handler_id: int) -> list[dict[str, list[str]]] | None:
        """Extract security requirements from handler middleware.

        Also accumulates seen scheme types for _register_security_schemes.

        Args:
            handler_id: Handler ID.

        Returns:
            List of SecurityRequirement objects or None.
        """
        middleware_meta = self.api._handler_middleware.get(handler_id, {})
        auth_config = middleware_meta.get("_auth_backend_instances")

        if not auth_config:
            return None

        security: list[dict[str, list[str]]] = []
        for backend in auth_config:
            scheme = backend.scheme_name
            openapi_name = _SCHEME_NAME_MAP.get(scheme)
            if openapi_name:
                security.append({openapi_name: []})
                self._seen_schemes.add(scheme)
                if scheme == "api_key" and self._api_key_header is None:
                    self._api_key_header = backend.header

        return security or None

    def _extract_tags(self, handler: Any) -> list[str] | None:
        """Extract tags for grouping operations.

        Args:
            handler: Handler function.

        Returns:
            List of tag names or None.
        """
        # Use module name as tag
        if hasattr(handler, "__module__"):
            module_parts = handler.__module__.split(".")
            if len(module_parts) > 0:
                # Use last part of module name (e.g., "users" from "myapp.api.users")
                tag = module_parts[-1]
                if tag == "api" and len(module_parts) > 1:
                    # If last part is "api", use the second-to-last part
                    # e.g., "users.api" -> "users"
                    tag = module_parts[-2]
                if tag != "api":  # Skip generic "api" tag
                    return [tag.capitalize()]

        return None

    def _collect_tags(self, collected_tag_names: set[str]) -> list[Tag] | None:
        """Collect and merge tags from operations with config tags.

        Args:
            collected_tag_names: Set of tag names collected from operations.

        Returns:
            List of Tag objects or None if no tags.
        """
        if not collected_tag_names and not self.config.tags:
            return None

        # Start with existing tags from config
        tag_objects: dict[str, Tag] = {}
        if self.config.tags:
            for tag in self.config.tags:
                tag_objects[tag.name] = tag

        # Add tags from operations (if not already defined in config)
        for tag_name in sorted(collected_tag_names):
            if tag_name not in tag_objects:
                # Create Tag object with just the name (no description)
                tag_objects[tag_name] = Tag(name=tag_name)

        # Return sorted list of Tag objects
        return list(tag_objects.values()) if tag_objects else None

    def _type_to_schema(self, type_annotation: Any, register_component: bool = False) -> Schema | Reference:
        """Convert a Python type annotation or msgspec.inspect node to an OpenAPI Schema.

        Args:
            type_annotation: Python type annotation or ``msgspec.inspect`` node.
            register_component: Whether to register complex types as components.

        Returns:
            Schema or Reference object.
        """
        if type_annotation is None or type_annotation == inspect._empty:
            return Schema(type="object")

        # msgspec.inspect nodes (Metadata, IntType, StructType, ...) dispatch by
        # class name; nodes without a handler (AnyType, NoneType, RawType, ...)
        # are a generic object. Everything else is a ``typing`` annotation.
        node_cls = type(type_annotation)
        if node_cls.__module__ == "msgspec.inspect":
            node_handler = _MSGSPEC_NODE_HANDLERS.get(node_cls.__name__)
            if node_handler is None:
                return Schema(type="object")
            return node_handler(self, type_annotation, register_component)
        return self._typing_to_schema(type_annotation, register_component)

    def _typing_to_schema(self, type_annotation: Any, register_component: bool) -> Schema | Reference:
        """Convert a raw ``typing`` annotation (not a msgspec.inspect node)."""
        # A documented custom type (e.g. ``Email = Annotated[str, Meta(...)]``)
        # used directly as a response model — bare or nested (``list[Email]``) —
        # still carries its msgspec Meta. Normalize it through msgspec.inspect so
        # it renders identically to the same type used as a Struct field. Param
        # markers like Query() are not msgspec.Meta, so ``Annotated[T, Query()]``
        # unwraps to ``T`` below. (#235)
        if get_origin(type_annotation) is Annotated:
            args = get_args(type_annotation)
            if any(isinstance(m, msgspec.Meta) for m in args[1:]):
                return self._type_to_schema(
                    msgspec.inspect.type_info(type_annotation), register_component=register_component
                )
            type_annotation = args[0]

        # Optional[T] -> T (single non-None arg only; multi-arm unions like
        # ``A | B | None`` go to _union_schema so every arm is preserved).
        if is_optional(type_annotation):
            non_none_args = [arg for arg in get_args(type_annotation) if arg is not type(None)]
            if len(non_none_args) == 1:
                type_annotation = non_none_args[0]

        origin_handler = _TYPING_ORIGIN_HANDLERS.get(get_origin(type_annotation))
        if origin_handler is not None:
            return origin_handler(self, get_args(type_annotation), register_component)

        if type_annotation is UploadFile:
            return Schema(type="string", format="binary")
        # Bare or parametrized Struct (``Page[UserRead]`` is keyed per parametrization).
        if _struct_origin(type_annotation) is not None:
            if register_component:
                return self._struct_to_component_schema(type_annotation)
            return self._struct_to_schema(type_annotation)
        # Bare enum classes (``-> GateReason``): same promote-or-inline policy
        # as the msgspec EnumType path. (#246)
        if isinstance(type_annotation, type) and issubclass(type_annotation, enum.Enum):
            return self._enum_schema(type_annotation, register_component=register_component)
        primitive = _PRIMITIVE_SCHEMAS.get(type_annotation)
        return Schema(**primitive) if primitive is not None else Schema(type="object")

    def _struct_to_schema(self, struct_type: type, *, register_component: bool = False) -> Schema:
        """Convert msgspec.Struct to inline OpenAPI Schema.

        For tagged unions (``msgspec.Struct, tag=...``), msgspec injects the
        tag/tag_field on the wire but they're not in ``struct_info.fields``.
        We surface them here as an ``enum=[tag]`` property so the schema
        round-trips correctly through Swagger UI examples and generated
        clients can use the field as a discriminator. Matches Litestar's
        ``StructSchemaPlugin`` behaviour.

        ``title`` is set to the struct class name so Swagger UI labels
        ``oneOf`` arms with the variant name (e.g. ``PostActivity``)
        instead of the positional fallback (``#0``, ``#1``, ``#2``).

        ``description`` is carried from the struct's (cleaned) ``__doc__``,
        matching the shape ``msgspec.json.schema_components`` produces. This
        is what ``openapi-typescript`` and similar codegen tools surface as
        JSDoc on generated types — without it every consumer-side type loses
        its hover-documentation.

        Args:
            struct_type: msgspec.Struct type.
            register_component: Whether nested complex types (enums, structs)
                should be registered as named components + ``$ref`` rather than
                inlined. True for body/response schemas, False for inline use.

        Returns:
            Schema object.
        """
        struct_info = msgspec.inspect.type_info(struct_type)
        properties = {}
        required = []

        for field in struct_info.fields:
            field_name, field_schema, field_required = self._msgspec_field_schema(
                field, register_component=register_component
            )
            properties[field_name] = field_schema

            # Check if required
            if field_required:
                required.append(field_name)

        tag_field = getattr(struct_info, "tag_field", None)
        tag = getattr(struct_info, "tag", None)
        if tag_field and tag is not None:
            properties[tag_field] = self._enum_values_schema([tag])
            required.append(tag_field)

        # Pull `description` from the struct's *own* docstring only (see
        # _own_docstring — avoids inheriting msgspec.Struct's base docstring).
        # Matches `msgspec.json.schema_components` behavior.
        return Schema(
            title=_type_display_name(struct_type),
            type="object",
            description=self._own_docstring(_struct_origin(struct_type) or struct_type),
            properties=properties,
            required=required or None,
        )

    def _register_component(self, cls: type, build_schema: Callable[[], Schema]) -> Reference:
        """Register ``cls`` as a component (by type identity) and return its shared Reference.

        Keyed by the type, not its name: the actual ``$ref`` string is left empty
        until ``_finalize_component_names`` runs, so two same-named types from
        different modules can both be registered here and later disambiguated
        instead of one stealing the other's name. Returns the *same* Reference
        instance on every call for a given type, so stamping its ``ref`` once at
        finalize time updates every use site — including refs nested inside
        ``allOf``/``anyOf`` wrappers built by ``_with_default``/``_union_schema``.

        The Reference is stored *before* ``build_schema`` runs so that a
        self-referential type (e.g. ``TreeNode`` with ``children: list[TreeNode]``)
        re-entering this method gets the existing Reference instead of recursing
        infinitely.
        """
        ref = self._component_ref.get(cls)
        if ref is not None:
            return ref
        ref = Reference(ref="")  # named in _finalize_component_names
        self._component_ref[cls] = ref
        self._component_schema[cls] = build_schema()
        return ref

    def _struct_to_component_schema(self, struct_type: type) -> Reference:
        """Convert msgspec.Struct to a component schema and return its reference."""
        return self._register_component(
            struct_type,
            lambda: self._struct_to_schema(struct_type, register_component=True),
        )

    def _enum_to_component_schema(self, enum_cls: type) -> Reference:
        """Register a named enum class as a component schema and return its reference.

        Parallels ``_struct_to_component_schema``: named enums (``enum.Enum`` /
        msgspec ``EnumType`` / Django ``TextChoices``/``IntegerChoices``) become
        reusable ``#/components/schemas/<Name>`` entries + ``$ref`` so codegen
        tools can emit a shared type, and the enum's docstring survives as
        ``description``. The narrowest fitting ``type`` is inferred from the
        member values, and ``title`` is set to the (short) class name — matching
        the shape ``msgspec.json.schema_components`` produces for enums.
        """

        def build() -> Schema:
            schema = self._enum_values_schema([e.value for e in enum_cls])
            # Read the enum's *own* docstring (not an inherited one), like structs.
            # Title stays the short class name even when the component is keyed by a
            # qualified name on collision, matching msgspec.
            return replace(schema, title=enum_cls.__name__, description=self._own_docstring(enum_cls))

        return self._register_component(enum_cls, build)

    def _finalize_component_names(self) -> None:
        """Assign final names to every registered component and populate self.schemas.

        Two-pass naming, mirroring ``msgspec.json.schema_components``: a component
        keeps its short ``__name__`` unless another *distinct* type shares it, in
        which case every colliding type expands to its normalized
        ``module.qualname`` so all of them coexist. Each type's shared Reference is
        stamped in place, updating all of its use sites at once.
        """

        def normalize(name: str) -> str:
            return re.sub(r"[^a-zA-Z0-9.\-_]", "_", name)

        def fullname(cls: type) -> str:
            origin = get_origin(cls) or cls
            display = _type_display_name(cls)
            return normalize(f"{origin.__module__}.{display.replace(origin.__name__, origin.__qualname__, 1)}")

        # First map name -> type, expanding only the names that actually collide.
        names: dict[str, type] = {}
        conflicts: set[str] = set()

        def assign(name: str, cls: type) -> None:
            existing = names.get(name)
            if existing is not None and existing is not cls:
                # Even the qualified names match (same module + qualname) — the
                # types are genuinely indistinguishable by name. Fail loudly
                # rather than emit a $ref that resolves to the wrong shape.
                raise ComponentNameCollisionError(name, cls, existing)
            names[name] = cls

        for cls in self._component_ref:
            short = normalize(_type_display_name(cls))
            if short in names and names[short] is not cls:
                # First collision on this short name: re-home the incumbent under
                # its qualified name and mark the short name as conflicted.
                incumbent = names.pop(short)
                conflicts.add(short)
                assign(fullname(incumbent), incumbent)
            if short in conflicts:
                assign(fullname(cls), cls)
            else:
                assign(short, cls)

        self._qualified_components = [fullname(names[n]) for n in sorted(names) if n not in conflicts and "." in n]

        # Stamp each shared Reference and publish the schema under its final name.
        for name, cls in names.items():
            self._component_ref[cls].ref = f"#/components/schemas/{name}"
            self.schemas[name] = self._component_schema[cls]


# --- _type_to_schema dispatch tables ---------------------------------------
#
# Handlers receive ``(generator, node_or_args, register_component)``. The
# msgspec table is keyed by the ``msgspec.inspect`` node class name; the typing
# table is keyed by ``get_origin(annotation)`` and receives ``get_args``.


def _node_metadata(gen: SchemaGenerator, node: Any, register: bool) -> Schema | Reference:
    # ``Annotated[T, Meta(...)]`` with informational fields (title/description/
    # examples/extra_json_schema) arrives as a ``Metadata`` wrapper: ``.type`` is
    # the constrained ``*Type``; ``.extra_json_schema`` holds the docs. Every
    # documented custom type in ``serializers.types`` takes this path. (#235)
    base = gen._type_to_schema(node.type, register_component=register)
    extra = getattr(node, "extra_json_schema", None)
    # Constraints already live on ``base``; docs only merge onto an inline
    # Schema — a $ref to a component cannot take siblings.
    if extra and isinstance(base, Schema):
        gen._apply_json_schema_extra(base, extra)
    return base


def _node_str(gen: SchemaGenerator, node: Any, register: bool) -> Schema:
    return Schema(
        **gen._schema_kwargs(
            type="string", min_length=node.min_length, max_length=node.max_length, pattern=node.pattern
        )
    )


def _node_struct(gen: SchemaGenerator, node: Any, register: bool) -> Schema | Reference:
    # Always a component so self-referential types emit a $ref instead of
    # recursing forever; _struct_to_component_schema guards re-entry.
    return gen._struct_to_component_schema(node.cls)


def _node_union(gen: SchemaGenerator, node: Any, register: bool) -> Schema | Reference:
    # String comparison: msgspec.inspect.NoneType is not builtins.NoneType.
    types = list(node.types)
    non_none = [t for t in types if type(t).__name__ != "NoneType"]
    has_none = len(non_none) != len(types)
    if not non_none:
        return Schema(type="null") if has_none else Schema(type="object")
    inner = [gen._type_to_schema(t, register_component=register) for t in non_none]
    return gen._union_schema(inner, has_none=has_none, tagged=_is_tagged_struct_union(non_none))


def _node_list(gen: SchemaGenerator, node: Any, register: bool) -> Schema:
    item_type = getattr(node, "item_type", None)
    if item_type:
        return Schema(type="array", items=gen._type_to_schema(item_type, register_component=register))
    return Schema(type="array", items=Schema(type="object"))


def _node_dict(gen: SchemaGenerator, node: Any, register: bool) -> Schema:
    # An untyped value (bare dict / dict[str, Any] -> AnyType) stays
    # additionalProperties: true.
    value_type = getattr(node, "value_type", None)
    typed = value_type is not None and type(value_type).__name__ != "AnyType"
    return gen._mapping_schema(gen._type_to_schema(value_type, register_component=register) if typed else None)


def _node_enum(gen: SchemaGenerator, node: Any, register: bool) -> Schema | Reference:
    # EnumType for plain enums; CustomType for Django TextChoices/IntegerChoices
    # (a metaclass msgspec does not see as a standard enum). Named enums become
    # components + $ref in body contexts and stay inline for params; anonymous
    # ``Literal[...]`` (LiteralType) always stays inline.
    cls = getattr(node, "cls", None)
    if cls is not None and issubclass(cls, enum.Enum):
        return gen._enum_schema(cls, register_component=register)
    return Schema(type="object")


_MSGSPEC_NODE_HANDLERS: dict[str, Callable[[SchemaGenerator, Any, bool], Schema | Reference]] = {
    "Metadata": _node_metadata,
    "IntType": lambda gen, node, _r: gen._numeric_type_schema(node, "integer"),
    "FloatType": lambda gen, node, _r: gen._numeric_type_schema(node, "number"),
    "StrType": _node_str,
    "BoolType": lambda _g, _n, _r: Schema(type="boolean"),
    "BytesType": lambda _g, _n, _r: Schema(type="string", format="binary"),
    "DateTimeType": lambda _g, _n, _r: Schema(type="string", format="date-time"),
    "DateType": lambda _g, _n, _r: Schema(type="string", format="date"),
    "TimeType": lambda _g, _n, _r: Schema(type="string", format="time"),
    "UUIDType": lambda _g, _n, _r: Schema(type="string", format="uuid"),
    "StructType": _node_struct,
    "UnionType": _node_union,
    "ListType": _node_list,
    "DictType": _node_dict,
    "EnumType": _node_enum,
    "CustomType": _node_enum,
    "LiteralType": lambda gen, node, _r: gen._enum_values_schema(node.values),
}


def _origin_union(gen: SchemaGenerator, args: tuple[Any, ...], register: bool) -> Schema | Reference:
    non_none = [arg for arg in args if arg is not type(None)]
    inner = [gen._type_to_schema(arg, register_component=register) for arg in non_none]
    return gen._union_schema(inner, has_none=len(non_none) != len(args), tagged=_is_tagged_struct_union(non_none))


def _origin_list(gen: SchemaGenerator, args: tuple[Any, ...], register: bool) -> Schema:
    item_type = args[0] if args else Any
    return Schema(type="array", items=gen._type_to_schema(item_type, register_component=register))


def _origin_dict(gen: SchemaGenerator, args: tuple[Any, ...], register: bool) -> Schema:
    # dict[K] without a value type or dict[str, Any] stays additionalProperties: true.
    value_type = args[1] if len(args) == 2 else None
    typed = value_type is not None and value_type is not Any
    return gen._mapping_schema(gen._type_to_schema(value_type, register_component=register) if typed else None)


_TYPING_ORIGIN_HANDLERS: dict[Any, Callable[[SchemaGenerator, tuple[Any, ...], bool], Schema | Reference]] = {
    Union: _origin_union,
    UnionType: _origin_union,
    list: _origin_list,
    dict: _origin_dict,
    # Bare typing.Literal does not go through msgspec.inspect.type_info.
    Literal: lambda gen, args, _r: gen._enum_values_schema(args),
}

# Kwargs, not Schema instances: callers may mutate the returned Schema.
_PRIMITIVE_SCHEMAS: dict[Any, dict[str, str]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    bytes: {"type": "string", "format": "binary"},
}
