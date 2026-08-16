"""Derive a queryset loading plan from a Serializer's declared fields.

A Serializer says *what* it reads from a model (columns, FK/O2O relations,
M2M/reverse relations, nested serializers, ``Config.annotations``). This module
turns that into the ORM calls that load it in a bounded number of queries:

- single-valued relations (``forward_one`` / ``reverse_one``) → ``select_related``
- many-valued relations (``many``) → ``prefetch_related`` (with a nested
  ``Prefetch`` queryset when the field is a nested Serializer list)
- ``Config.annotations`` → ``annotate``
- Django >= 6.1: ``fetch_mode(FETCH_PEERS)`` as a safety net for anything not
  declared (e.g. a model ``@property`` reading a FK)

Plans are built once per ``(serializer, model)``; the cache holds both classes
weakly so dev reloads don't pin stale classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from django.db.models import Prefetch, QuerySet
from django.db.models.query import ModelIterable

try:  # Django >= 6.1
    from django.db.models import FETCH_ONE, FETCH_PEERS
except ImportError:  # pragma: no cover - older Django
    FETCH_ONE = FETCH_PEERS = None

if TYPE_CHECKING:
    from .base import Serializer

__all__ = ("LoadingPlan", "build_loading_plan")


@dataclass(frozen=True)
class LoadingPlan:
    """The ORM calls a Serializer needs to load its fields without extra queries."""

    select_related: tuple[str, ...] = ()
    prefetch: tuple[str | Prefetch, ...] = ()
    annotations: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.select_related or self.prefetch or self.annotations)

    def apply(self, queryset: QuerySet) -> QuerySet:
        """Return ``queryset`` with this plan applied.

        Additive and idempotent: relations the caller already loads are kept
        (``select_related`` merges; a prefetch lookup already present is not
        added again), annotations already on the query are not overwritten.
        A ``values()``/``values_list()`` queryset yields rows, not model
        instances — there is nothing to load onto, so it is returned unchanged.
        """
        if not issubclass(queryset._iterable_class, ModelIterable):
            return queryset
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch:
            existing = {_prefetch_lookup(lookup) for lookup in queryset._prefetch_related_lookups}
            missing = [lookup for lookup in self.prefetch if _prefetch_lookup(lookup) not in existing]
            if missing:
                queryset = queryset.prefetch_related(*missing)
        if self.annotations:
            present = queryset.query.annotations
            missing_annotations = {k: v for k, v in self.annotations.items() if k not in present}
            if missing_annotations:
                queryset = queryset.annotate(**missing_annotations)
        if FETCH_PEERS is not None and queryset._fetch_mode is FETCH_ONE:
            queryset = queryset.fetch_mode(FETCH_PEERS)
        return queryset


def _prefetch_lookup(lookup: str | Prefetch) -> str:
    return lookup.prefetch_to if isinstance(lookup, Prefetch) else lookup


_PLAN_CACHE: WeakKeyDictionary[type, WeakKeyDictionary[type, LoadingPlan]] = WeakKeyDictionary()


def build_loading_plan(serializer_cls: type[Serializer], model_cls: type) -> LoadingPlan:
    """Build (or fetch the cached) loading plan for ``serializer_cls`` over ``model_cls``."""
    per_model = _PLAN_CACHE.get(model_cls)
    if per_model is None:
        per_model = _PLAN_CACHE[model_cls] = WeakKeyDictionary()
    plan = per_model.get(serializer_cls)
    if plan is None:
        # Guard against recursion (Author.posts -> Post.author -> ...): publish an
        # empty plan first; a cycle stops at the already-declared level.
        per_model[serializer_cls] = LoadingPlan()
        plan = per_model[serializer_cls] = _build(serializer_cls, model_cls)
    return plan


def _build(serializer_cls: type[Serializer], model_cls: type) -> LoadingPlan:
    from .base import _get_django_relation_info  # noqa: PLC0415 - avoid import cycle

    serializer_cls._ensure_from_model_ready()
    select_related: list[str] = []
    prefetch: list[str | Prefetch] = []
    seen_prefetch: set[str] = set()

    def add_prefetch(lookup: str | Prefetch) -> None:
        name = _prefetch_lookup(lookup)
        if name not in seen_prefetch:
            seen_prefetch.add(name)
            prefetch.append(lookup)

    for spec in serializer_cls.__model_field_specs__:
        source = spec.source or spec.field_name
        parts = source.split(".")
        current_model = model_cls
        path: list[str] = []
        for index, part in enumerate(parts):
            info = _get_django_relation_info(current_model, part)
            if info is None or info.related_model is None:
                break  # column, property, method: nothing to load
            path.append(part)
            lookup = "__".join(path)
            is_last = index == len(parts) - 1
            nested = spec.nested if is_last else None

            if info.kind == "many":
                nested_qs = None
                if nested is not None:
                    nested_plan = build_loading_plan(nested.serializer_class, info.related_model)
                    if nested_plan:
                        nested_qs = nested_plan.apply(info.related_model._default_manager.all())
                add_prefetch(Prefetch(lookup, queryset=nested_qs) if nested_qs is not None else lookup)
                break  # cannot select_related through a many-valued relation

            if is_last and nested is None and info.kind == "forward_one":
                break  # from_model reads the FK id (attname); the related row is never touched
            select_related.append(lookup)
            if nested is not None:
                nested_plan = build_loading_plan(nested.serializer_class, info.related_model)
                select_related.extend(f"{lookup}__{rel}" for rel in nested_plan.select_related)
                for nested_lookup in nested_plan.prefetch:
                    if isinstance(nested_lookup, Prefetch):
                        add_prefetch(
                            Prefetch(f"{lookup}__{nested_lookup.prefetch_to}", queryset=nested_lookup.queryset)
                        )
                    else:
                        add_prefetch(f"{lookup}__{nested_lookup}")
            current_model = info.related_model

    config = getattr(serializer_cls, "Config", None)
    annotations = dict(getattr(config, "annotations", None) or {})

    return LoadingPlan(
        select_related=tuple(dict.fromkeys(select_related)),
        prefetch=tuple(prefetch),
        annotations=annotations,
    )
