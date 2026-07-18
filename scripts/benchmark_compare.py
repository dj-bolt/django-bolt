#!/usr/bin/env python3
"""Compare benchmark markdown files with deterministic pass/fail gates.

Gates on BOTH throughput (Reqs/sec) and tail latency (p99): throughput alone
can hide tail regressions (e.g. a change that batches work may keep RPS flat
while pushing p99 up).
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

REQS_RE = re.compile(r"Reqs/sec\s+([0-9.]+)")
P99_RE = re.compile(r"99%\s+([0-9.]+)(us|ms|s)\b")

CORE_ENDPOINT_KEYS = (
    "Root Endpoint Performance",
    "Items GET Performance (/items/1?q=hello)",
    "10kb JSON (Async) (/10k-json)",
    "10kb JSON (Sync) (/sync-10k-json)",
    "Header Endpoint (/header)",
    "Cookie Endpoint (/cookie)",
)

_UNIT_TO_US = {"us": 1.0, "ms": 1000.0, "s": 1_000_000.0}


def parse_bench(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    """Parse (reqs_per_sec, p99_latency_us) per benchmark section."""
    reqs: dict[str, float] = {}
    p99: dict[str, float] = {}
    current: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            current = line[4:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()

        if current is None:
            continue

        match = REQS_RE.search(line)
        if match:
            reqs[current] = float(match.group(1))

        match = P99_RE.search(line)
        if match:
            p99[current] = float(match.group(1)) * _UNIT_TO_US[match.group(2)]

    return reqs, p99


def pct_delta(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="bench/BENCHMARK_BASELINE.md")
    parser.add_argument("--candidate", default="bench/BENCHMARK_DEV.md")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=2.0,
        help="Maximum allowed RPS regression percentage per endpoint.",
    )
    parser.add_argument(
        "--max-p99-regression",
        type=float,
        default=25.0,
        help=(
            "Maximum allowed p99 latency increase (%%) per endpoint. Tail "
            "latency is noisier than throughput, so the default is looser."
        ),
    )
    parser.add_argument(
        "--core-median-min-gain",
        type=float,
        default=15.0,
        help="Minimum required median gain (%) across core endpoints.",
    )
    args = parser.parse_args()

    baseline, baseline_p99 = parse_bench(Path(args.baseline))
    candidate, candidate_p99 = parse_bench(Path(args.candidate))

    common = sorted(set(baseline).intersection(candidate))
    if not common:
        print("ERROR: no comparable benchmark entries found", file=sys.stderr)
        return 2

    regressions: list[tuple[str, float, float, float]] = []
    for key in common:
        old = baseline[key]
        new = candidate[key]
        delta = pct_delta(old, new)
        if delta < -args.max_regression:
            regressions.append((key, old, new, delta))

    p99_common = sorted(set(baseline_p99).intersection(candidate_p99))
    p99_regressions: list[tuple[str, float, float, float]] = []
    for key in p99_common:
        old = baseline_p99[key]
        new = candidate_p99[key]
        delta = pct_delta(old, new)  # positive = p99 got WORSE
        if delta > args.max_p99_regression:
            p99_regressions.append((key, old, new, delta))

    core_deltas: list[float] = []
    missing_core: list[str] = []
    for key in CORE_ENDPOINT_KEYS:
        if key not in baseline or key not in candidate:
            missing_core.append(key)
            continue
        core_deltas.append(pct_delta(baseline[key], candidate[key]))

    if missing_core:
        print("ERROR: missing core benchmark keys:")
        for key in missing_core:
            print(f"  - {key}")
        return 2

    core_median_gain = statistics.median(core_deltas)

    print(f"Compared endpoints: {len(common)} (RPS), {len(p99_common)} (p99)")
    print(f"Core median gain: {core_median_gain:.2f}%")

    failed = False

    if regressions:
        failed = True
        print(f"FAIL: {len(regressions)} endpoint(s) regressed RPS by more than {args.max_regression:.2f}%:")
        for key, old, new, delta in sorted(regressions, key=lambda x: x[3]):
            print(f"  - {key}: {old:.2f} -> {new:.2f} ({delta:.2f}%)")

    if p99_regressions:
        failed = True
        print(
            f"FAIL: {len(p99_regressions)} endpoint(s) regressed p99 latency "
            f"by more than {args.max_p99_regression:.2f}%:"
        )
        for key, old, new, delta in sorted(p99_regressions, key=lambda x: -x[3]):
            print(f"  - {key}: {old:.0f}us -> {new:.0f}us (+{delta:.2f}%)")

    if failed:
        return 1

    if core_median_gain < args.core_median_min_gain:
        print(f"FAIL: core median gain below target ({core_median_gain:.2f}% < {args.core_median_min_gain:.2f}%)")
        return 1

    print("PASS: no RPS/p99 endpoint exceeded regression thresholds and core median gain target was met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
