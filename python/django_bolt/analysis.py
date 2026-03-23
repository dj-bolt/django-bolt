"""
Static analysis module for handler functions.

Performs AST-based analysis of handler source code to detect:
- Django ORM usage patterns
- Blocking I/O operations

This enables compile-time optimization decisions (e.g., running sync handlers
with ORM usage in a thread pool) and developer warnings.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HandlerAnalysis",
    "analyze_handler",
]


# Django ORM manager attributes
ORM_MANAGER_ATTRS = frozenset(
    {
        "objects",
        "_default_manager",
        "_base_manager",
    }
)

# Sync ORM methods on QuerySet/Manager (blocking)
SYNC_ORM_METHODS = frozenset(
    {
        # QuerySet evaluation (blocking)
        "all",
        "filter",
        "exclude",
        "get",
        "first",
        "last",
        "earliest",
        "latest",
        "create",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "bulk_update",
        "update",
        "delete",
        "count",
        "exists",
        "aggregate",
        "annotate",
        "values",
        "values_list",
        "distinct",
        "order_by",
        "select_related",
        "prefetch_related",
        "only",
        "defer",
        "using",
        "raw",
        "extra",
    }
)

# Model instance methods that hit the database (blocking)
# These are detected separately since they're called on model instances, not managers
MODEL_INSTANCE_METHODS = frozenset(
    {
        "save",
        "delete",
        "refresh_from_db",
        "full_clean",
        "clean",
        "validate_unique",
    }
)

# Async model instance methods (Django 4.1+)
ASYNC_MODEL_INSTANCE_METHODS = frozenset(
    {
        "asave",
        "adelete",
        "arefresh_from_db",
    }
)

# Async ORM methods on QuerySet/Manager (Django 4.1+)
ASYNC_ORM_METHODS = frozenset(
    {
        "aget",
        "afirst",
        "alast",
        "aearliest",
        "alatest",
        "acreate",
        "aget_or_create",
        "aupdate_or_create",
        "abulk_create",
        "abulk_update",
        "aupdate",
        "adelete",
        "acount",
        "aexists",
        "aaggregate",
        "ain_bulk",
        "aiterator",
        "acontains",
        "aexplain",
    }
)

# QuerySet iteration patterns (blocking when used synchronously)
QUERYSET_ITERATION_PATTERNS = frozenset(
    {
        "__iter__",
        "__len__",
        "__bool__",
        "__getitem__",
    }
)

# Blocking I/O operations
BLOCKING_IO_FUNCTIONS = frozenset(
    {
        # File operations
        "open",
        "read",
        "write",
        "close",
        # Network
        "urlopen",
        "request",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        # Time
        "sleep",
        # Subprocess
        "run",
        "call",
        "check_output",
        "check_call",
        "Popen",
    }
)

# Blocking I/O modules
BLOCKING_IO_MODULES = frozenset(
    {
        "requests",
        "urllib",
        "urllib3",
        "httpx",  # sync client
        "socket",
        "subprocess",
        "os",
        "io",
        "time",
    }
)


@dataclass
class HandlerAnalysis:
    """
    Result of static analysis on a handler function.

    Contains information about ORM usage and blocking operations
    to determine if sync handlers should run in a thread pool.
    """

    # ORM detection
    uses_orm: bool = False
    """Whether handler accesses Django ORM"""

    orm_operations: set[str] = field(default_factory=set)
    """Set of ORM operations detected (e.g., {'filter', 'get', 'all'})"""

    # Blocking I/O detection
    has_blocking_io: bool = False
    """Whether handler has potential blocking I/O calls"""

    blocking_operations: set[str] = field(default_factory=set)
    """Set of blocking operations detected"""

    # Request component access detection
    request_needs_body: bool = False
    """Whether the handler reads request.body"""

    request_needs_query: bool = False
    """Whether the handler reads request.query or query-derived helpers"""

    request_needs_headers: bool = False
    """Whether the handler reads request.headers or header-derived helpers"""

    request_needs_cookies: bool = False
    """Whether the handler reads request.cookies"""

    # Analysis metadata
    analysis_failed: bool = False
    """Whether AST analysis failed (e.g., couldn't get source)"""

    failure_reason: str | None = None
    """Reason for analysis failure if any"""

    @property
    def is_blocking(self) -> bool:
        """Whether handler is likely to block (any ORM usage or blocking I/O)."""
        return self.uses_orm or self.has_blocking_io

    def get_warning_message(self, handler_name: str, path: str, is_async: bool) -> str | None:
        """
        Generate warning message if handler has potential issues.

        Only warns for sync handlers that use ORM - they will be run in a thread pool.
        Async handlers don't need warnings - Django/Python handles sync ORM automatically.

        Args:
            handler_name: Name of the handler function
            path: Route path
            is_async: Whether handler is defined as async

        Returns:
            Warning message string or None if no warning needed
        """
        # Only warn for sync handlers that use ORM operations
        # These will be automatically run in a thread pool to avoid blocking
        if not is_async and self.uses_orm:
            ops = ", ".join(sorted(self.orm_operations)[:5])
            if len(self.orm_operations) > 5:
                ops += f", ... ({len(self.orm_operations)} total)"

            return (
                f"Sync handler '{handler_name}' at {path} uses ORM operations "
                f"(detected: {ops}). Running in thread pool."
            )

        return None


class OrmVisitor(ast.NodeVisitor):
    """
    AST visitor that detects Django ORM usage patterns.

    Looks for:
    - Model.objects.method() calls
    - QuerySet method chains
    - Model instance save/delete calls

    To reduce false positives, we only flag ORM methods when:
    1. We've seen .objects manager access in the function, OR
    2. The method is called directly on .objects (e.g., User.objects.get())
    """

    def __init__(self, request_param_names: set[str] | None = None) -> None:
        self.analysis = HandlerAnalysis()
        self._in_objects_chain = False
        self.request_param_names = request_param_names or set()

    def _is_request_name(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.request_param_names

    def _mark_request_component_attr(self, attr_name: str) -> None:
        if attr_name == "body":
            self.analysis.request_needs_body = True
        elif attr_name == "query":
            self.analysis.request_needs_query = True
        elif attr_name == "headers":
            self.analysis.request_needs_headers = True
        elif attr_name == "cookies":
            self.analysis.request_needs_cookies = True
        elif attr_name == "META":
            self.analysis.request_needs_headers = True
            self.analysis.request_needs_query = True
        elif attr_name == "get_full_path":
            self.analysis.request_needs_query = True
        elif attr_name == "build_absolute_uri":
            self.analysis.request_needs_headers = True
            self.analysis.request_needs_query = True

    def _mark_request_component_key(self, key: str) -> None:
        if key in {"body", "query", "headers", "cookies", "META", "get_full_path", "build_absolute_uri"}:
            self._mark_request_component_attr(key)

    def _check_for_objects_chain(self, node: ast.AST) -> bool:
        """
        Check if node is part of an .objects chain.

        Returns True if the attribute chain contains .objects
        (e.g., User.objects.filter() or queryset.filter())
        """
        current = node
        while isinstance(current, ast.Attribute):
            if current.attr in ORM_MANAGER_ATTRS:
                return True
            current = current.value

        # Also check for Call nodes in the chain (e.g., User.objects.filter().first())
        while isinstance(current, ast.Call):
            if isinstance(current.func, ast.Attribute):
                if current.func.attr in ORM_MANAGER_ATTRS:
                    return True
                # Check if the call is on a queryset method
                if current.func.attr in SYNC_ORM_METHODS or current.func.attr in ASYNC_ORM_METHODS:
                    # Recurse to check if the chain includes .objects
                    return self._check_for_objects_chain(current.func.value)
                current = current.func.value
            else:
                break

        return False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Detect attribute access patterns like Model.objects.filter()."""
        attr_name = node.attr

        if self._is_request_name(node.value):
            self._mark_request_component_attr(attr_name)

        # Check for .objects manager access
        if attr_name in ORM_MANAGER_ATTRS:
            self.analysis.uses_orm = True

        # Check for ORM method calls - ONLY if in an .objects chain
        # This reduces false positives like dict.get(), response.get(), etc.
        is_in_objects_chain = self._check_for_objects_chain(node)

        if is_in_objects_chain and (attr_name in SYNC_ORM_METHODS or attr_name in ASYNC_ORM_METHODS):
            self.analysis.uses_orm = True
            self.analysis.orm_operations.add(attr_name)

        # Check for model instance methods (save, delete, etc.)
        # These are called on model instances, not managers
        if attr_name in MODEL_INSTANCE_METHODS or attr_name in ASYNC_MODEL_INSTANCE_METHODS:
            self.analysis.uses_orm = True
            self.analysis.orm_operations.add(attr_name)

        # Continue visiting children
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Detect function calls that might be blocking."""
        # Check for direct blocking function calls
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in BLOCKING_IO_FUNCTIONS:
                self.analysis.has_blocking_io = True
                self.analysis.blocking_operations.add(func_name)

        # Check for method calls on blocking modules
        elif isinstance(node.func, ast.Attribute):
            if self._is_request_name(node.func.value):
                self._mark_request_component_attr(node.func.attr)
                if (
                    node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    self._mark_request_component_key(node.args[0].value)

            # e.g., requests.get(), time.sleep()
            if isinstance(node.func.value, ast.Name):
                module_name = node.func.value.id
                method_name = node.func.attr

                if module_name in BLOCKING_IO_MODULES:
                    self.analysis.has_blocking_io = True
                    self.analysis.blocking_operations.add(f"{module_name}.{method_name}")

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Detect request['headers'] style access."""
        if (
            self._is_request_name(node.value)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            self._mark_request_component_key(node.slice.value)

        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        """Detect iteration over QuerySets (blocking)."""
        # Check if iterating over something that looks like a QuerySet
        # e.g., for user in User.objects.all():
        if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute):
            method_name = node.iter.func.attr
            if method_name in SYNC_ORM_METHODS or method_name in ASYNC_ORM_METHODS:
                self.analysis.uses_orm = True
                self.analysis.orm_operations.add(f"iterate_{method_name}")

        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """Detect list comprehension over QuerySets."""
        # [user.name for user in User.objects.all()]
        for generator in node.generators:
            if isinstance(generator.iter, ast.Call) and isinstance(generator.iter.func, ast.Attribute):
                method_name = generator.iter.func.attr
                if method_name in SYNC_ORM_METHODS or method_name in ASYNC_ORM_METHODS:
                    self.analysis.uses_orm = True
                    self.analysis.orm_operations.add(f"comprehension_{method_name}")

        self.generic_visit(node)


def analyze_handler(
    fn: Callable[..., Any],
    request_param_names: set[str] | None = None,
) -> HandlerAnalysis:
    """
    Analyze a handler function for ORM usage and blocking operations.

    Uses AST parsing to statically analyze the handler source code
    and detect patterns that would block the event loop.

    Args:
        fn: The handler function to analyze

    Returns:
        HandlerAnalysis with detected patterns
    """
    analysis = HandlerAnalysis()

    unwraped_source = inspect.unwrap(fn)
    # Try to get source code
    try:
        source = inspect.getsource(unwraped_source)
    except (OSError, TypeError) as e:
        # Can't get source (e.g., built-in, C extension, or lambda)
        analysis.analysis_failed = True
        analysis.failure_reason = f"Could not get source: {e}"
        return analysis

    # Dedent source code (handles indented methods)
    source = textwrap.dedent(source)

    # Parse AST
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        analysis.analysis_failed = True
        analysis.failure_reason = f"Syntax error parsing source: {e}"
        return analysis

    # Find the function definition and analyze only its body (not decorators)
    # This prevents false positives from decorator names like @api.delete("/m")
    visitor = OrmVisitor(request_param_names=request_param_names)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only analyze the function body, not decorators
            for stmt in node.body:
                visitor.visit(stmt)
            break  # Only analyze the first function found

    return visitor.analysis


def warn_blocking_handler(
    fn: Callable[..., Any],
    path: str,
    is_async: bool,
    analysis: HandlerAnalysis | None = None,
) -> None:
    """
    Emit warning if handler has blocking operations.

    Args:
        fn: Handler function
        path: Route path
        is_async: Whether handler is async
        analysis: Pre-computed analysis (will compute if None)
    """
    if analysis is None:
        analysis = analyze_handler(fn)

    if analysis.analysis_failed:
        return

    warning_msg = analysis.get_warning_message(fn.__name__, path, is_async)
    if warning_msg:
        warnings.warn(warning_msg, stacklevel=3)
