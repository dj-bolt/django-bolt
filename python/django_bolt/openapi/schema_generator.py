from __future__ import annotations

import enum
import http.client
import inspect
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

import msgspec

from ..typing import is_msgspec_struct, is_optional
from .spec import (
    OpenAPI,
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

__all__ = ("SchemaGenerator",)

# Mapping from auth backend scheme_name to OpenAPI security scheme identifier
_SCHEME_NAME_MAP: dict[str, str] = {
    "jwt": "BearerAuth",
    "api_key": "ApiKeyAuth",
}


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
        self.schemas: dict[str, Schema] = {}  # Component schemas registry

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

    def _msgspec_field_schema(
        self, field: Any, *, register_component: bool = False
    ) -> tuple[str, Schema | Reference, bool]:
        """Build a schema and required flag for a msgspec-inspected field."""
        field_name = field.encode_name
        field_schema = self._type_to_schema(field.type, register_component=register_component)
        field_required = field.required and field.default is msgspec.NODEFAULT

        if field.default is not msgspec.NODEFAULT:
            field_schema = self._with_default(field_schema, field.default)

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

            if path not in paths:
                paths[path] = PathItem()

            # Get handler metadata
            meta = self.api._handler_meta.get(handler_id, {})

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

        # Auto-register security schemes from auth backends used on routes
        self._register_security_schemes(openapi)

        # Add component schemas
        if self.schemas:
            openapi.components.schemas = self.schemas

        # Collect and merge tags
        openapi.tags = self._collect_tags(collected_tags)

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

        # Fallback to docstring if not explicitly set
        if (summary is None or description is None) and self.config.use_handler_docstrings and handler.__doc__:
            doc = inspect.cleandoc(handler.__doc__)
            lines = doc.split("\n", 1)
            if summary is None:
                summary = lines[0]
            if description is None and len(lines) > 1:
                description = lines[1].strip()

        # Extract parameters
        parameters = self._extract_parameters(meta, path)

        # Extract request body
        request_body = self._extract_request_body(meta)

        # Extract responses (pass handler_id for auth error responses)
        responses = self._extract_responses(meta, handler_id)

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

        # Fallback to docstring if not explicitly set
        if (summary is None or description is None) and self.config.use_handler_docstrings and handler.__doc__:
            doc = inspect.cleandoc(handler.__doc__)
            lines = doc.split("\n", 1)
            if summary is None:
                summary = lines[0]
            if description is None and len(lines) > 1:
                description = lines[1].strip()

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
        # Document the 101 Switching Protocols response
        responses = {
            "101": OpenAPIResponse(
                description="Switching Protocols - WebSocket connection established",
                headers={
                    "Upgrade": Parameter(
                        name="Upgrade",
                        param_in="header",
                        schema=Schema(type="string", enum=["websocket"]),
                    ),
                    "Connection": Parameter(
                        name="Connection",
                        param_in="header",
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
                properties = {}
                required = []
                for field in form_fields:
                    # Access FieldDefinition attributes directly
                    name = field.alias or field.name
                    annotation = field.annotation
                    default = field.default
                    source = field.source

                    if source == "file":
                        # File upload
                        schema = Schema(type="string", format="binary")
                    else:
                        schema = self._type_to_schema(annotation)

                    properties[name] = schema

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

    def _extract_responses(self, meta: dict[str, Any], handler_id: int) -> dict[str, OpenAPIResponse]:
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
                        content={"application/json": OpenAPIMediaType(schema=schema)},
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
                        content={"application/json": OpenAPIMediaType(schema=schema)},
                    )
            # fall through to error response logic below
        else:
            # Single-response mode (existing behavior, unchanged)
            # Get response type
            response_type = meta.get("response_type")
            default_status = meta.get("default_status_code", 200)

            # Add successful response
            if response_type and response_type != inspect._empty:
                schema = self._type_to_schema(response_type, register_component=True)

                responses[str(default_status)] = OpenAPIResponse(
                    description="Successful response",
                    content={
                        "application/json": OpenAPIMediaType(schema=schema),
                    },
                )
            else:
                # Default response
                responses["200"] = OpenAPIResponse(
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
        """Convert Python type annotation to OpenAPI Schema.

        Args:
            type_annotation: Python type annotation.
            register_component: Whether to register complex types as components.

        Returns:
            Schema or Reference object.
        """
        # Handle None/empty
        if type_annotation is None or type_annotation == inspect._empty:
            return Schema(type="object")

        # Handle msgspec type info objects (IntType, StrType, BoolType, etc.)
        type_name = type(type_annotation).__name__
        if hasattr(type_annotation, "__class__") and type_name.endswith("Type"):
            # Numeric types with constraint support (ge/gt/le/lt/multiple_of)
            if type_name == "IntType":
                return self._numeric_type_schema(type_annotation, "integer")
            if type_name == "FloatType":
                return self._numeric_type_schema(type_annotation, "number")
            # String type with constraint support (min_length/max_length/pattern)
            if type_name == "StrType":
                return Schema(
                    **self._schema_kwargs(
                        type="string",
                        min_length=type_annotation.min_length,
                        max_length=type_annotation.max_length,
                        pattern=type_annotation.pattern,
                    )
                )
            # Types without constraints — static map
            msgspec_type_map = {
                "BoolType": Schema(type="boolean"),
                "BytesType": Schema(type="string", format="binary"),
                "DateTimeType": Schema(type="string", format="date-time"),
                "DateType": Schema(type="string", format="date"),
                "TimeType": Schema(type="string", format="time"),
                "UUIDType": Schema(type="string", format="uuid"),
            }
            if type_name in msgspec_type_map:
                return msgspec_type_map[type_name]
            # For list/array types from msgspec
            if type_name == "StructType":
                # Nested struct from msgspec.inspect — always register as a
                # component so self-referential types emit a $ref instead of
                # recursing infinitely.  The sentinel in
                # _struct_to_component_schema guards against re-entry.
                return self._struct_to_component_schema(type_annotation.cls)
            if type_name == "UnionType" and hasattr(type_annotation, "types"):
                # msgspec.inspect UnionType — the .types attr distinguishes
                # this from Python's built-in types.UnionType (which uses
                # .__args__ instead).
                non_none_types = [
                    t
                    for t in type_annotation.types
                    # String comparison because msgspec.inspect.NoneType is
                    # not the same object as builtins.NoneType.
                    if type(t).__name__ != "NoneType"
                ]
                if len(non_none_types) == 1:
                    return self._type_to_schema(non_none_types[0], register_component=register_component)
                if len(non_none_types) > 1:
                    return Schema(
                        any_of=[self._type_to_schema(t, register_component=register_component) for t in non_none_types]
                    )
                return Schema(type="object")
            if type_name == "ListType":
                item_type = getattr(type_annotation, "item_type", None)
                if item_type:
                    item_schema = self._type_to_schema(item_type, register_component=register_component)
                    return Schema(type="array", items=item_schema)
                return Schema(type="array", items=Schema(type="object"))
            # For dict types from msgspec
            if type_name == "DictType":
                return Schema(type="object", additional_properties=True)
            # For enum types from msgspec (EnumType for plain enums,
            # CustomType for Django TextChoices/IntegerChoices which use
            # a metaclass that msgspec doesn't recognise as a standard enum)
            if (
                type_name in ("EnumType", "CustomType")
                and hasattr(type_annotation, "cls")
                and issubclass(type_annotation.cls, enum.Enum)
            ):
                values = [e.value for e in type_annotation.cls]
                return self._enum_values_schema(values)
            # msgspec.inspect.type_info represents Literal fields on Structs as
            # LiteralType, so keep this branch alongside the bare typing.Literal
            # branch below.
            if type_name == "LiteralType":
                return self._enum_values_schema(type_annotation.values)

        # Unwrap Optional
        origin = get_origin(type_annotation)
        args = get_args(type_annotation)

        if origin is Annotated:
            # Unwrap Annotated[T, ...]
            type_annotation = args[0]
            origin = get_origin(type_annotation)
            args = get_args(type_annotation)

        # Handle Optional[T] -> T
        if is_optional(type_annotation):
            # Get the non-None type
            non_none_args = [arg for arg in args if arg is not type(None)]
            if non_none_args:
                type_annotation = non_none_args[0]
                origin = get_origin(type_annotation)
                args = get_args(type_annotation)

        # Handle msgspec.Struct
        if is_msgspec_struct(type_annotation):
            if register_component:
                return self._struct_to_component_schema(type_annotation)
            else:
                return self._struct_to_schema(type_annotation)

        # Handle list
        if origin is list:
            item_type = args[0] if args else Any
            item_schema = self._type_to_schema(item_type, register_component=register_component)
            return Schema(type="array", items=item_schema)

        # Handle dict
        if origin is dict:
            return Schema(type="object", additional_properties=True)

        # Bare typing.Literal annotations don't come through
        # msgspec.inspect.type_info, so they need their own path here.
        if origin is Literal:
            return self._enum_values_schema(args)

        # Handle primitive types
        type_map = {
            str: Schema(type="string"),
            int: Schema(type="integer"),
            float: Schema(type="number"),
            bool: Schema(type="boolean"),
            bytes: Schema(type="string", format="binary"),
        }

        for py_type, schema in type_map.items():
            if type_annotation == py_type:
                return schema

        # Default to generic object
        return Schema(type="object")

    def _struct_to_schema(self, struct_type: type) -> Schema:
        """Convert msgspec.Struct to inline OpenAPI Schema.

        Args:
            struct_type: msgspec.Struct type.

        Returns:
            Schema object.
        """
        struct_info = msgspec.inspect.type_info(struct_type)
        properties = {}
        required = []

        for field in struct_info.fields:
            field_name, field_schema, field_required = self._msgspec_field_schema(field, register_component=False)
            properties[field_name] = field_schema

            # Check if required
            if field_required:
                required.append(field_name)

        return Schema(
            type="object",
            properties=properties,
            required=required or None,
        )

    def _struct_to_component_schema(self, struct_type: type) -> Reference:
        """Convert msgspec.Struct to component schema and return reference.

        Args:
            struct_type: msgspec.Struct type.

        Returns:
            Reference to component schema.
        """
        schema_name = struct_type.__name__

        # Check if already registered (or currently being processed)
        if schema_name not in self.schemas:
            # Insert a sentinel *before* processing fields so that
            # self-referential types (e.g. TreeNode with children:
            # list[TreeNode]) hit the guard on re-entry instead of
            # recursing infinitely.  The sentinel is overwritten once
            # _struct_to_schema returns the real schema.
            self.schemas[schema_name] = Schema(type="object")
            self.schemas[schema_name] = self._struct_to_schema(struct_type)

        return Reference(ref=f"#/components/schemas/{schema_name}")
