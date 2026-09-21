#!/usr/bin/env python3
"""Check the declared support matrix against upstream release schedules.

``pyproject.toml``'s classifiers are the single source of truth for which Python
and Django versions django-bolt supports: the CI test matrix is derived from
them (see the ``python-versions`` job in ``.github/workflows/CI.yml``). This
script keeps that source of truth honest by comparing it against
`endoflife.date <https://endoflife.date>`_, which tracks both upstreams'
release and end-of-life dates.

It reports drift in either direction:

* a declared version upstream no longer supports (it went EOL -- drop it),
* a maintained Django series that is not declared (a new release shipped),
* a ``Django>=`` floor that disagrees with the oldest declared series,
* a declared Django series that does not support the oldest declared Python,
  which would invalidate the pairing the derived CI matrix assumes.

Python is checked in one direction only: EOL interpreters must not be declared,
but the floor is deliberately higher than Python's own EOL schedule (Django 6.x
requires 3.12+), so a maintained-but-undeclared Python is not drift.

Usage::

    python scripts/check_support_matrix.py            # human-readable report
    python scripts/check_support_matrix.py --format md  # markdown, for an issue

Transient API failures are retried with exponential backoff, so a momentary blip
does not report a broken check.

Exit status is 0 when the matrix is current, 1 when it has drifted, and 2 when
the check itself could not run (unreachable API, unexpected response shape), so a
scheduled workflow can tell a finding apart from a broken check.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

API = "https://endoflife.date/api/v1/products/{product}"
TIMEOUT = 30

# Transient-failure retry. Nobody is waiting on the scheduled run, so a blip at
# endoflife.date should cost a few seconds of backoff rather than a spurious
# "the check itself broke" notification. Only failures that a retry could
# plausibly fix are retried -- a 404 means the product moved and will 404 again.
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0  # seconds, doubled per attempt
BACKOFF_CAP = 30.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Exit codes. Drift is a finding, not a failure, so it gets its own code and
# operational errors get another -- a caller must not mistake an unreachable
# API for "nothing has drifted", nor for drift.
EXIT_CURRENT = 0
EXIT_DRIFTED = 1
EXIT_ERROR = 2

PYTHON_PREFIX = "Programming Language :: Python :: "
DJANGO_PREFIX = "Framework :: Django :: "

REPO_ROOT = Path(__file__).resolve().parent.parent


class Drift(NamedTuple):
    """One discrepancy between what we declare and what upstream supports."""

    subject: str
    detail: str
    fix: str


def die(message: str) -> NoReturn:
    """Abort with the operational-error status, separate from the drift status."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in v.split("."))


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header -- delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait after a failed ``attempt`` (1-based).

    A server-supplied ``Retry-After`` wins, capped so a hostile or mistaken
    value cannot stall the job. Otherwise exponential backoff with full jitter.
    """
    if (supplied := _retry_after_seconds(retry_after)) is not None:
        return min(supplied, BACKOFF_CAP)
    ceiling = min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_CAP)
    # Full jitter: spreads retries if several jobs hit the API at once.
    return ceiling * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not crypto


def fetch_releases(product: str, max_attempts: int = MAX_ATTEMPTS) -> list[dict[str, Any]]:
    """Return endoflife.date's release records for ``product``, newest first.

    Retries transient failures -- connection errors, timeouts, 5xx/429, and
    truncated bodies -- with exponential backoff. A permanent failure (4xx, or a
    response whose shape we do not recognise) aborts on the first attempt.
    """
    url = API.format(product=product)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https URL
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in RETRYABLE_STATUS:
                die(f"{url} returned {last_error}")
            retry_after = exc.headers.get("Retry-After")
        except (urllib.error.URLError, TimeoutError) as exc:
            # DNS failure, connection refused or reset, TLS hiccup, timeout.
            last_error = f"{type(exc).__name__}: {exc}"
        except json.JSONDecodeError as exc:
            # Usually a truncated body or an intercepting proxy's error page.
            last_error = f"invalid JSON: {exc}"
        else:
            releases = payload.get("result", {}).get("releases")
            if not releases:
                die(f"{url} returned no releases; the API shape may have changed")
            return releases

        if attempt < max_attempts:
            delay = _backoff_delay(attempt, retry_after)
            print(
                f"warning: {url}: {last_error}; retrying in {delay:.1f}s (attempt {attempt} of {max_attempts})",
                file=sys.stderr,
            )
            time.sleep(delay)

    die(f"could not reach {url} after {max_attempts} attempts; last error: {last_error}")


def maintained(releases: list[dict[str, Any]]) -> list[str]:
    """Series still receiving upstream support (active or security-only)."""
    return sorted((r["name"] for r in releases if r.get("isMaintained")), key=_version_key)


def eol(releases: list[dict[str, Any]]) -> dict[str, str]:
    """Map each unmaintained series to the date its support ended."""
    return {r["name"]: r.get("eolFrom") or "an unknown date" for r in releases if not r.get("isMaintained")}


def supported_pythons(release: dict[str, Any]) -> tuple[str, str] | None:
    """Parse a Django release's ``3.10 - 3.14`` Python range, if stated.

    The field is prose and carries parentheticals such as ``(added in 5.2.8)``,
    so anything unrecognised yields ``None`` and the corresponding check is
    skipped rather than reported as drift.
    """
    raw = (release.get("custom") or {}).get("supportedPythonVersions")
    if not raw:
        return None
    match = re.match(r"\s*(\d+\.\d+)\s*-\s*(\d+\.\d+)", raw)
    if not match:
        return None
    return match.group(1), match.group(2)


def read_declared(pyproject: Path) -> tuple[list[str], list[str], str | None]:
    """Return (declared Pythons, declared Django series, ``Django>=`` floor)."""
    with pyproject.open("rb") as f:
        project = tomllib.load(f)["project"]

    classifiers = project["classifiers"]
    pythons = sorted(
        (
            c.removeprefix(PYTHON_PREFIX)
            for c in classifiers
            # "... :: 3" is the language-level marker; "... :: Implementation ::
            # CPython" and the Free Threading marker are not versions either.
            if c.startswith(PYTHON_PREFIX) and re.fullmatch(r"\d+\.\d+", c.removeprefix(PYTHON_PREFIX))
        ),
        key=_version_key,
    )
    djangos = sorted(
        (
            c.removeprefix(DJANGO_PREFIX)
            for c in classifiers
            if c.startswith(DJANGO_PREFIX) and re.fullmatch(r"\d+\.\d+", c.removeprefix(DJANGO_PREFIX))
        ),
        key=_version_key,
    )

    floor = None
    for dep in project.get("dependencies", []):
        if match := re.fullmatch(r"(?i)django\s*>=\s*([\d.]+)", dep.strip()):
            floor = match.group(1)
            break

    if not pythons:
        die("no 'Programming Language :: Python :: X.Y' classifiers in pyproject.toml")
    if not djangos:
        die("no 'Framework :: Django :: X.Y' classifiers in pyproject.toml")
    return pythons, djangos, floor


def check(pyproject: Path, max_attempts: int = MAX_ATTEMPTS) -> tuple[list[Drift], list[str]]:
    """Compare the declared matrix against upstream. Returns (drift, notes)."""
    pythons, djangos, floor = read_declared(pyproject)

    django_releases = fetch_releases("django", max_attempts)
    python_releases = fetch_releases("python", max_attempts)

    drift: list[Drift] = []
    notes: list[str] = []

    # --- Django: both directions ---
    django_maintained = maintained(django_releases)
    django_eol = eol(django_releases)

    for version in djangos:
        if version in django_eol:
            drift.append(
                Drift(
                    f"Django {version}",
                    f"end-of-life since {django_eol[version]}, but still declared as supported",
                    f'remove "{DJANGO_PREFIX}{version}" from the pyproject classifiers',
                )
            )
    for version in django_maintained:
        if version not in djangos:
            drift.append(
                Drift(
                    f"Django {version}",
                    "still supported upstream, but not declared",
                    f'add "{DJANGO_PREFIX}{version}" to the pyproject classifiers',
                )
            )

    # --- the Django>= floor must match the oldest declared series ---
    oldest_declared = djangos[0]
    if floor is None:
        notes.append("could not find a `Django>=X.Y` entry in [project].dependencies; floor not checked")
    elif _version_key(floor) != _version_key(oldest_declared):
        drift.append(
            Drift(
                f"Django>={floor}",
                f"disagrees with the oldest declared series (Django {oldest_declared})",
                f'set the dependency to "Django>={oldest_declared}"',
            )
        )

    # --- Python: EOL interpreters only ---
    python_eol = eol(python_releases)
    for version in pythons:
        if version in python_eol:
            drift.append(
                Drift(
                    f"Python {version}",
                    f"end-of-life since {python_eol[version]}, but still declared as supported",
                    f'remove "{PYTHON_PREFIX}{version}" from the pyproject classifiers',
                )
            )

    # --- the CI matrix pairs every declared Django with the oldest Python ---
    oldest_python = pythons[0]
    by_name = {r["name"]: r for r in django_releases}
    for version in djangos:
        release = by_name.get(version)
        if release is None:
            notes.append(f"Django {version} is declared but unknown to endoflife.date; pairing not checked")
            continue
        window = supported_pythons(release)
        if window is None:
            notes.append(f"Django {version}: upstream Python range not machine-readable; pairing not checked")
            continue
        low, high = window
        if not (_version_key(low) <= _version_key(oldest_python) <= _version_key(high)):
            drift.append(
                Drift(
                    f"Django {version} + Python {oldest_python}",
                    f"Django {version} supports Python {low} - {high}, "
                    f"which excludes the oldest declared Python ({oldest_python})",
                    "the derived CI matrix pairs older Django series with the oldest Python; "
                    "pin this series to a compatible Python in the test matrix instead",
                )
            )

    return drift, notes


def render(drift: list[Drift], notes: list[str], fmt: str) -> str:
    lines: list[str] = []
    if fmt == "md":
        if drift:
            lines.append("The declared support matrix has drifted from upstream release schedules.")
            lines.append("")
            lines.append("| Subject | Problem | Suggested fix |")
            lines.append("| --- | --- | --- |")
            lines += [f"| `{d.subject}` | {d.detail} | {d.fix} |" for d in drift]
            lines.append("")
            lines.append(
                "The CI test matrix is derived from the `pyproject.toml` classifiers, "
                "so updating them updates CI too. Also refresh the version lists in "
                "`README.md`, `CLAUDE.md`, and `docs/src/faq.md`."
            )
        else:
            lines.append("The declared support matrix matches upstream release schedules.")
        if notes:
            lines.append("")
            lines.append("Notes:")
            lines += [f"- {n}" for n in notes]
    else:
        if drift:
            lines.append(f"Support matrix has drifted ({len(drift)} problem(s)):")
            for d in drift:
                lines.append(f"  - {d.subject}: {d.detail}")
                lines.append(f"    fix: {d.fix}")
        else:
            lines.append("Support matrix is current: declared versions match upstream release schedules.")
        for n in notes:
            lines.append(f"  note: {n}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=REPO_ROOT / "pyproject.toml",
        help="path to pyproject.toml (default: the repo root's)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "md"),
        default="text",
        help="output format; 'md' renders a table suitable for a GitHub issue body",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_ATTEMPTS,
        metavar="N",
        help=f"attempts per API request before giving up (default: {MAX_ATTEMPTS}); 1 disables retries",
    )
    args = parser.parse_args()
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")

    drift, notes = check(args.pyproject, args.max_attempts)
    print(render(drift, notes, args.format))
    return EXIT_DRIFTED if drift else EXIT_CURRENT


if __name__ == "__main__":
    sys.exit(main())
