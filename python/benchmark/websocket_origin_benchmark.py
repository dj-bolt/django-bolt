"""Repeatable native Rust WebSocket routing/Origin performance comparison.

Run the SAME script on a built upstream baseline and on the fix branch:
    uv run python python/benchmark/websocket_origin_benchmark.py --output /tmp/ws-before.json
    uv run python python/benchmark/websocket_origin_benchmark.py --output /tmp/ws-after.json --compare /tmp/ws-before.json

The test app is registered once. Measurements call the real Rust
_core.handle_test_websocket function; no network or Python WebSocket handler
is executed. Repeat on the same machine with release builds and low load.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import inspect
import json
import math
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import django
from django.conf import settings
from django.core.management import call_command

if not settings.configured:
    settings.configure(
        SECRET_KEY="websocket-origin-benchmark-only",
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "django_bolt"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        ROOT_URLCONF="__main__",
        BOLT_API=["__main__:network_api"],
        ALLOWED_HOSTS=["*"],
        CORS_ALLOWED_ORIGINS=["https://allowed.example"],
    )
django.setup()

from django_bolt import BoltAPI, WebSocket, _core  # noqa: E402
from django_bolt.testing import WebSocketTestClient  # noqa: E402


async def echo_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    while (await receive())["type"] != "websocket.disconnect":
        pass


def custom_origin(origin, context):
    return origin == "https://allowed.example"


def p95(samples):
    """Use the nearest-rank percentile for both batch means and individual calls."""
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def measure(path: str, policy: str, count: int, repeats: int) -> dict:
    api = BoltAPI()
    native_options = {}
    if path == "/native/ws" and policy == "allowlist":
        native_options = {"origin_policy": "allowlist", "allowed_origins": ["https://allowed.example"]}
    elif path == "/native/ws" and policy == "any":
        native_options = {"origin_policy": "any"}

    @api.websocket("/native/ws", **native_options)
    async def native(websocket: WebSocket):
        await websocket.accept()

    mount_kwargs = {}
    if policy == "delegate":
        mount_kwargs["websocket_origin_policy"] = "delegate"
    elif policy == "any":
        mount_kwargs["websocket_origin_policy"] = "any"
    elif policy == "custom":
        mount_kwargs["websocket_origin_policy"] = "custom"
        mount_kwargs["websocket_origin_validator"] = custom_origin
    elif policy == "allowlist":
        mount_kwargs["websocket_origin_policy"] = "allowlist"
        mount_kwargs["websocket_allowed_origins"] = ["https://allowed.example"]
    api.mount_asgi("/mounted", echo_app, **mount_kwargs)
    client = WebSocketTestClient(
        api,
        path,
        headers={"Origin": "https://allowed.example"},
        cors_allowed_origins=["https://allowed.example"],
        read_django_settings=False,
    )
    app_id = client._get_or_create_app_id()
    args = (app_id, path, [("origin", "https://allowed.example")], None)
    try:
        for _ in range(500):
            _core.handle_test_websocket(*args)
        samples = []
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            for _ in range(repeats):
                start = time.perf_counter_ns()
                for _ in range(count):
                    _core.handle_test_websocket(*args)
                samples.append((time.perf_counter_ns() - start) / count)
        finally:
            if was_enabled:
                gc.enable()
        return {
            "path": path,
            "policy": policy,
            "median_ns_per_call": round(statistics.median(samples)),
            "p95_batch_ns_per_call": round(p95(samples)),
            "samples_ns_per_call": [round(s) for s in samples],
        }
    finally:
        client._cleanup_app()


async def full_handshake_roundtrips(policy: str, count: int) -> dict:
    """Measure in-process registration, policy resolution, accept, and cleanup.

    This includes TestClient setup for each connection. It excludes TCP,
    HTTP parsing, and the production server's HTTP 101 response.
    """
    api = BoltAPI()
    kwargs = {}
    if policy == "allowlist":
        kwargs = {
            "websocket_origin_policy": "allowlist",
            "websocket_allowed_origins": ["https://allowed.example"],
        }
    elif policy == "delegate":
        kwargs = {"websocket_origin_policy": "delegate"}
    elif policy == "any":
        kwargs = {"websocket_origin_policy": "any"}
    elif policy == "custom":
        kwargs = {"websocket_origin_policy": "custom", "websocket_origin_validator": custom_origin}
    api.mount_asgi("/mounted", echo_app, **kwargs)
    client_args = {
        "headers": {"Origin": "https://allowed.example"},
        "cors_allowed_origins": ["https://allowed.example"],
        "read_django_settings": False,
    }
    samples = []
    for iteration in range(count + 15):
        start = time.perf_counter_ns()
        async with WebSocketTestClient(api, "/mounted/ws", **client_args) as ws:
            assert ws.accepted
        elapsed = time.perf_counter_ns() - start
        if iteration >= 15:
            samples.append(elapsed)
    return {
        "policy": policy,
        "median_ns_per_connection": round(statistics.median(samples)),
        "p95_ns_per_connection": round(p95(samples)),
    }


def network_cases():
    # Keep endpoint counts, paths, and request order identical in both builds.
    # Upstream serves the new scenarios with its inherited CORS check.
    cases = [("native", "inherit"), ("mount", "inherit")]
    cases.extend(
        (target, policy)
        for target in ("native", "mount")
        for policy in ("allowlist", "any", "custom_sync", "custom_async")
    )
    cases.append(("mount", "delegate"))
    return cases


def serve_network(port):
    """Start one real runbolt worker with equivalent benchmark endpoints."""
    global network_api
    network_api = BoltAPI()

    @network_api.get("/health")
    async def health():
        return {"ok": True}

    async def async_origin(origin, context):
        return custom_origin(origin, context)

    def add_case(target, policy):
        options = {}
        if policy == "allowlist":
            options = {"origin_policy": "allowlist", "allowed_origins": ["https://allowed.example"]}
        elif policy in ("any", "delegate"):
            options = {"origin_policy": policy}
        elif policy.startswith("custom_"):
            options = {
                "origin_policy": "custom",
                "origin_validator": custom_origin if policy == "custom_sync" else async_origin,
            }
        if "origin_policy" not in inspect.signature(BoltAPI.websocket).parameters:
            options = {}
        path = f"/{target}/{policy}"
        if target == "native":

            @network_api.websocket(path, **options)
            async def native(websocket: WebSocket):
                await websocket.accept()
                async for _ in websocket.iter_text():
                    pass
        else:
            network_api.mount_asgi(path, echo_app, **{f"websocket_{key}": value for key, value in options.items()})

    for target, policy in network_cases():
        add_case(target, policy)
    call_command(
        "runbolt", host="127.0.0.1", port=port, workers=1, processes=1, no_admin=True, no_color=True, skip_checks=True
    )


def measure_network(count, repeats):
    """Measure TCP connect through receipt of the real HTTP 101 headers.

    Each request uses a fresh socket. Teardown is outside the timed region.
    Requests run sequentially. No TLS, proxy, application messages, or auth run.
    """
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryFile(mode="w+") as server_log:
        server = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)],
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if server.poll() is not None:
                    server_log.seek(0)
                    raise RuntimeError(f"runbolt stopped during startup: {server_log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2) as probe:
                        probe.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                        if b" 200 " in probe.recv(1024):
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("runbolt did not become ready")
                time.sleep(0.05)

            cases = network_cases()
            requests = {
                (target, policy): (
                    f"GET /{target}/{policy} HTTP/1.1\r\nHost: localhost:{port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                    "Origin: https://allowed.example\r\n\r\n"
                ).encode("ascii")
                for target, policy in cases
            }

            def handshake(case):
                start = time.perf_counter_ns()
                with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                    connection.sendall(requests[case])
                    response = b""
                    while b"\r\n\r\n" not in response:
                        chunk = connection.recv(4096)
                        if not chunk:
                            raise RuntimeError(f"Connection ended before HTTP 101: {response!r}")
                        response += chunk
                    elapsed = time.perf_counter_ns() - start
                    if b" 101 " not in response.split(b"\r\n", 1)[0]:
                        raise RuntimeError(f"Expected HTTP 101: {response!r}")
                    if b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" not in response:
                        raise RuntimeError("Invalid Sec-WebSocket-Accept")
                return elapsed

            samples = {case: [] for case in cases}
            batches = {case: [] for case in cases}
            for case in cases:
                for _ in range(50):
                    handshake(case)
            rng = random.Random(0)
            for _ in range(repeats):
                order = list(cases)
                rng.shuffle(order)
                for case in order:
                    batch = [handshake(case) for _ in range(count)]
                    samples[case].extend(batch)
                    batches[case].append({"median_ns": round(statistics.median(batch)), "p95_ns": round(p95(batch))})
            return [
                {
                    "target": target,
                    "policy": policy,
                    "effective_policy": policy
                    if "origin_policy" in inspect.signature(BoltAPI.websocket).parameters
                    else "inherit",
                    "count": len(samples[(target, policy)]),
                    "median_ns": round(statistics.median(samples[(target, policy)])),
                    "p95_ns": round(p95(samples[(target, policy)])),
                    "batches": batches[(target, policy)],
                    "samples_ns": samples[(target, policy)],
                }
                for target, policy in cases
            ]
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8001, help=argparse.SUPPRESS)
    parser.add_argument("--network-count", type=int, default=0, help="Handshakes per network batch; zero disables TCP")
    parser.add_argument("--network-repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--roundtrips", type=int, default=250)
    parser.add_argument("--repeats", type=int, default=9)
    args = parser.parse_args()
    if args.serve:
        serve_network(args.port)
        return
    if args.network_count < 0 or args.network_repeats < 1:
        parser.error("network-count must be nonnegative and network-repeats must be positive")
    if args.count < 1 or args.repeats < 1 or args.roundtrips < 1:
        parser.error("count, repeats and roundtrips must be positive")

    rows = [
        measure("/native/ws", "native", args.count, args.repeats),
        measure("/mounted/ws", "inherit", args.count, args.repeats),
    ]
    # The old baseline's API does not have the new mount options.
    if "websocket_origin_policy" in inspect.signature(BoltAPI.mount_asgi).parameters:
        rows.extend(
            [
                measure("/native/ws", "allowlist", args.count, args.repeats),
                measure("/native/ws", "any", args.count, args.repeats),
                measure("/mounted/ws", "allowlist", args.count, args.repeats),
                measure("/mounted/ws", "delegate", args.count, args.repeats),
                measure("/mounted/ws", "any", args.count, args.repeats),
            ]
        )
    roundtrips = [asyncio.run(full_handshake_roundtrips("inherit", args.roundtrips))]
    if "websocket_origin_policy" in inspect.signature(BoltAPI.mount_asgi).parameters:
        roundtrips.extend(
            [
                asyncio.run(full_handshake_roundtrips("allowlist", args.roundtrips)),
                asyncio.run(full_handshake_roundtrips("delegate", args.roundtrips)),
                asyncio.run(full_handshake_roundtrips("any", args.roundtrips)),
                asyncio.run(full_handshake_roundtrips("custom", args.roundtrips)),
            ]
        )
    network = measure_network(args.network_count, args.network_repeats) if args.network_count else []
    result = {
        "network": network,
        "network_measurement": "Sequential TCP connect to HTTP 101 headers; excludes teardown, TLS, messages, and authentication",
        "percentile": "nearest-rank; routing p95 uses batch means, network p95 uses individual handshakes",
        "loadavg": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "extension": str(_core.__file__),
        "measurement": "Rust test routing and in-process client lifecycle; excludes network handshakes",
        "roundtrip_count": args.roundtrips,
        "roundtrips": roundtrips,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "count": args.count,
        "repeats": args.repeats,
        "results": rows,
    }
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        base = {(row["path"], row["policy"]): row for row in baseline["results"]}
        print("\nCHANGE RELATIVE TO BASELINE:")
        for row in rows:
            key = (row["path"], row["policy"])
            if key in base:
                before = base[key]["median_ns_per_call"]
                after = row["median_ns_per_call"]
                old_p95 = base[key]["p95_batch_ns_per_call"]
                new_p95 = row["p95_batch_ns_per_call"]
                print(
                    f"{row['policy']}: median {before} -> {after} ns/call "
                    f"({(after / before - 1) * 100:+.2f}%); "
                    f"p95 batch {old_p95} -> {new_p95} ns/call "
                    f"({(new_p95 / old_p95 - 1) * 100:+.2f}%)"
                )
            else:
                print(f"{row['policy']}: new feature, no baseline counterpart")
        old_handshakes = {entry["policy"]: entry for entry in baseline.get("roundtrips", [])}
        for entry in roundtrips:
            old = old_handshakes.get(entry["policy"])
            if old:
                before = old["median_ns_per_connection"]
                after = entry["median_ns_per_connection"]
                old_p95 = old["p95_ns_per_connection"]
                new_p95 = entry["p95_ns_per_connection"]
                print(
                    f"in-process {entry['policy']}: median {before} -> {after} ns/connection "
                    f"({(after / before - 1) * 100:+.2f}%); "
                    f"p95 {old_p95} -> {new_p95} ns/connection "
                    f"({(new_p95 / old_p95 - 1) * 100:+.2f}%)"
                )
        old_network = {(entry["target"], entry["policy"]): entry for entry in baseline.get("network", [])}
        for entry in network:
            old = old_network.get((entry["target"], entry["policy"]))
            if old is None:
                continue
            before, after = old["median_ns"], entry["median_ns"]
            old_p95, new_p95 = old["p95_ns"], entry["p95_ns"]
            # New policy names were exercised with inherited CORS on upstream.
            # Label both effective policies rather than implying a like-for-like
            # comparison of a custom policy that did not exist upstream.
            policies = f"{old.get('effective_policy', 'inherit')} -> {entry.get('effective_policy', entry['policy'])}"
            print(
                f"network {entry['target']}/{entry['policy']} (effective {policies}): "
                f"median {before} -> {after} ns/handshake "
                f"({(after / before - 1) * 100:+.2f}%); "
                f"p95 {old_p95} -> {new_p95} ns/handshake "
                f"({(new_p95 / old_p95 - 1) * 100:+.2f}%)"
            )


if __name__ == "__main__":
    main()
