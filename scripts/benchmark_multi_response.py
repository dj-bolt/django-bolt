"""
Benchmark: Multi-Response Hot Path Overhead

Measures whether the multi-response code path adds measurable overhead
to the existing single-response hot path in serialize_response_sync.

Scenarios:
1. Single-response baseline (dict return) — existing handler path
2. Multi-response tuple return — tuple detection → _resolve_response_type → recurse
3. Multi-response bare dict return — _resolve_response_type for default status code
4. Multi-response JSON() return — JSON handler with status code resolution

See: https://github.com/dj-bolt/django-bolt/issues/151
"""

from __future__ import annotations

import timeit

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="benchmark-key",
        INSTALLED_APPS=["django_bolt"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    )
    django.setup()

from django_bolt import BoltAPI
from django_bolt.responses import JSON
from django_bolt.serialization import serialize_response_sync
from django_bolt.serializers import Serializer


# ============================================================================
# Test Schemas
# ============================================================================


class OkSchema(Serializer):
    id: int
    name: str


class ErrorSchema(Serializer):
    detail: str


# ============================================================================
# Build handler metadata via route registration
# ============================================================================


def build_single_response_meta():
    """Single response_model=OkSchema — the existing hot path."""
    api = BoltAPI()

    @api.get("/items", response_model=OkSchema)
    def list_items():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    return api._handler_meta[handler_id]


def build_multi_response_meta():
    """Multi response_model={200: Ok, 400: Err} — the new path."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    def get_item(item_id: int):
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    return api._handler_meta[handler_id]


# Pre-build metadata (not part of the benchmark)
SINGLE_META = build_single_response_meta()
MULTI_META = build_multi_response_meta()

# Pre-build test data
DICT_DATA = {"id": 1, "name": "Alice"}
TUPLE_DATA = (200, {"id": 1, "name": "Alice"})
ERROR_DATA = {"detail": "err"}
JSON_RESPONSE = JSON(ERROR_DATA, status_code=400)


# ============================================================================
# Benchmark helpers
# ============================================================================


def print_winner(baseline_time: float, other_time: float) -> None:
    """Print overhead percentage relative to baseline."""
    overhead = ((other_time - baseline_time) / baseline_time) * 100
    if overhead > 0:
        print(f"  Overhead vs baseline: +{overhead:.1f}%")
    else:
        print(f"  Overhead vs baseline: {overhead:.1f}% (faster)")


# ============================================================================
# Benchmarks
# ============================================================================


def run_benchmarks():
    iterations = 100_000

    print("=" * 80)
    print("BENCHMARK: Multi-Response Hot Path Overhead")
    print("=" * 80)
    print(f"\nIterations: {iterations:,}")
    print()

    # ------------------------------------------------------------------
    # 1. Single-response baseline (dict return)
    # ------------------------------------------------------------------
    print("-" * 80)
    print("1. Single-response baseline (dict return)")
    print("   serialize_response_sync({'id': 1, 'name': 'Alice'}, single_meta)")
    print("   Hot path: meta.get('is_multi_response', False) → False → direct")
    print("-" * 80)

    baseline_time = timeit.timeit(
        lambda: serialize_response_sync(DICT_DATA, SINGLE_META),
        number=iterations,
    )
    baseline_ops = iterations / baseline_time
    print(f"  Time: {baseline_time:.4f}s  ({baseline_ops:,.0f} ops/sec)")
    print()

    # ------------------------------------------------------------------
    # 2. Multi-response tuple return
    # ------------------------------------------------------------------
    print("-" * 80)
    print("2. Multi-response tuple return")
    print("   serialize_response_sync((200, {'id': 1, 'name': 'Alice'}), multi_meta)")
    print("   Path: tuple detection → _resolve_response_type → dict(meta) → recurse")
    print("-" * 80)

    tuple_time = timeit.timeit(
        lambda: serialize_response_sync(TUPLE_DATA, MULTI_META),
        number=iterations,
    )
    tuple_ops = iterations / tuple_time
    print(f"  Time: {tuple_time:.4f}s  ({tuple_ops:,.0f} ops/sec)")
    print_winner(baseline_time, tuple_time)
    print()

    # ------------------------------------------------------------------
    # 3. Multi-response bare dict return
    # ------------------------------------------------------------------
    print("-" * 80)
    print("3. Multi-response bare dict return")
    print("   serialize_response_sync({'id': 1, 'name': 'Alice'}, multi_meta)")
    print("   Path: is_multi → _resolve_response_type for default status → serialize")
    print("-" * 80)

    bare_dict_time = timeit.timeit(
        lambda: serialize_response_sync(DICT_DATA, MULTI_META),
        number=iterations,
    )
    bare_dict_ops = iterations / bare_dict_time
    print(f"  Time: {bare_dict_time:.4f}s  ({bare_dict_ops:,.0f} ops/sec)")
    print_winner(baseline_time, bare_dict_time)
    print()

    # ------------------------------------------------------------------
    # 4. Multi-response JSON() return
    # ------------------------------------------------------------------
    print("-" * 80)
    print("4. Multi-response JSON() return")
    print("   serialize_response_sync(JSON({'detail': 'err'}, status_code=400), multi_meta)")
    print("   Path: JSON isinstance → is_multi → _resolve_response_type → validate")
    print("-" * 80)

    # Recreate JSON each iteration to avoid measuring cached state
    json_time = timeit.timeit(
        lambda: serialize_response_sync(
            JSON({"detail": "err"}, status_code=400), MULTI_META
        ),
        number=iterations,
    )
    json_ops = iterations / json_time
    print(f"  Time: {json_time:.4f}s  ({json_ops:,.0f} ops/sec)")
    print_winner(baseline_time, json_time)
    print()

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    print(f"  {'Scenario':<40} {'ops/sec':>12} {'vs baseline':>12}")
    print(f"  {'-' * 40} {'-' * 12} {'-' * 12}")

    results = [
        ("1. Single-response baseline", baseline_ops, baseline_time),
        ("2. Multi-response tuple", tuple_ops, tuple_time),
        ("3. Multi-response bare dict", bare_dict_ops, bare_dict_time),
        ("4. Multi-response JSON()", json_ops, json_time),
    ]

    for name, ops, time in results:
        overhead = ((time - baseline_time) / baseline_time) * 100
        if name.startswith("1."):
            overhead_str = "—"
        elif overhead > 0:
            overhead_str = f"+{overhead:.1f}%"
        else:
            overhead_str = f"{overhead:.1f}%"
        print(f"  {name:<40} {ops:>12,.0f} {overhead_str:>12}")

    print()
    print(f"  Single-response hot path cost of meta.get('is_multi_response'):")
    print(f"  Already included in baseline — dict.get() on absent key is ~40ns.")
    print()


if __name__ == "__main__":
    run_benchmarks()
