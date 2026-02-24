#!/usr/bin/env python3
"""Compare benchmark markdown files with deterministic pass/fail gates."""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

REQS_RE = re.compile(r"Reqs/sec\s+([0-9.]+)")

CORE_ENDPOINT_KEYS = (
    "Root Endpoint Performance",
    "Items GET Performance (/items/1?q=hello)",
    "10kb JSON (Async) (/10k-json)",
    "10kb JSON (Sync) (/sync-10k-json)",
    "Header Endpoint (/header)",
    "Cookie Endpoint (/cookie)",
)


def parse_reqs(path: Path) -> dict[str, float]:
    entries: dict[str, float] = {}
    current: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            current = line[4:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()

        match = REQS_RE.search(line)
        if match and current:
            entries[current] = float(match.group(1))

    return entries


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
        help="Maximum allowed regression percentage per endpoint.",
    )
    parser.add_argument(
        "--core-median-min-gain",
        type=float,
        default=15.0,
        help="Minimum required median gain (%) across core endpoints.",
    )
    args = parser.parse_args()

    baseline = parse_reqs(Path(args.baseline))
    candidate = parse_reqs(Path(args.candidate))

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

    print(f"Compared endpoints: {len(common)}")
    print(f"Core median gain: {core_median_gain:.2f}%")

    if regressions:
        print(f"FAIL: {len(regressions)} endpoint(s) regressed by more than {args.max_regression:.2f}%:")
        for key, old, new, delta in sorted(regressions, key=lambda x: x[3]):
            print(f"  - {key}: {old:.2f} -> {new:.2f} ({delta:.2f}%)")
        return 1

    if core_median_gain < args.core_median_min_gain:
        print(f"FAIL: core median gain below target ({core_median_gain:.2f}% < {args.core_median_min_gain:.2f}%)")
        return 1

    print("PASS: no endpoint exceeded regression threshold and core median gain target was met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
