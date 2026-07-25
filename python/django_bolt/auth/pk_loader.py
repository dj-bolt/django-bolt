"""
Pre-compiled primary-key user loading.

Django recompiles the SELECT statement for every ``Model.objects.get(pk=...)``
call — as_sql/pre_sql_setup/add_q run per query. Profiled under load, that
compilation is roughly two thirds of the per-request cost of loading
``request.user``, and it happens on every authenticated request that touches
the user. Following the framework's do-it-once-at-registration principle, the
default user loaders compile the pk SELECT once per (model, database alias)
and execute it directly, materializing the instance with ``Model.from_db``
exactly as a QuerySet row would be.

The fast path only applies when the model's default manager uses the stock
``get_queryset``. A manager that overrides it may inject dynamic WHERE
clauses (time windows, tenancy, feature flags), which a one-time compile
would freeze; those models transparently fall back to a per-call
``manager.get(pk=...)`` — the exact behavior this module replaces.

This module is a leaf (imported by both ``backends`` and ``user_loader``) so
it must not import from either.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, models, router

# (model, db_alias) -> (select-by-pk SQL, concrete field attnames), or None
# when the model's manager customizes queryset construction and the loader
# must fall back to the ORM per call.
_pk_query_cache: dict[tuple[type, str], tuple[str, list[str]] | None] = {}


def _compiled_pk_query(model: type, alias: str) -> tuple[str, list[str]] | None:
    key = (model, alias)
    if key in _pk_query_cache:
        return _pk_query_cache[key]

    manager = model._default_manager
    if type(manager).get_queryset is not models.Manager.get_queryset:
        # Overridden get_queryset (directly or via a manager subclass) can
        # filter dynamically — never freeze it into a compiled statement.
        compiled = None
    else:
        # Same SELECT column order as a QuerySet row (concrete fields in
        # Meta order — what from_db expects), single %s param for the pk.
        query = manager.using(alias).filter(pk=1).query
        sql, _params = query.get_compiler(alias).as_sql()
        fields = [f.attname for f in model._meta.concrete_fields]
        compiled = (sql, fields)

    _pk_query_cache[key] = compiled
    return compiled


def load_user_by_pk_sync(model: type, user_id: Any) -> Any | None:
    """
    Load a user by primary key, skipping per-call SQL compilation when safe.

    Behaviorally equivalent to ``model._default_manager.get(pk=user_id)``
    returning ``None`` for a missing or malformed pk. Models whose default
    manager customizes ``get_queryset`` use that manager per call instead of
    the compiled statement.
    """
    pk_field = model._meta.pk
    try:
        pk_python = pk_field.to_python(user_id)
    except ValidationError:
        # Garbage/stale token subject — same outcome as DoesNotExist.
        return None

    alias = router.db_for_read(model) or DEFAULT_DB_ALIAS
    compiled = _compiled_pk_query(model, alias)
    if compiled is None:
        try:
            return model._default_manager.using(alias).get(pk=pk_python)
        except model.DoesNotExist:
            return None

    connection = connections[alias]
    try:
        pk_value = pk_field.get_db_prep_value(pk_python, connection)
    except (ValueError, TypeError):
        return None

    sql, fields = compiled
    with connection.cursor() as cursor:
        cursor.execute(sql, (pk_value,))
        row = cursor.fetchone()
    if row is None:
        return None
    return model.from_db(alias, fields, row)
