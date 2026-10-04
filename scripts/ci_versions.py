"""Print the CI matrix values that pyproject.toml defines, as GITHUB_OUTPUT lines.

The Python and Django versions come from the trove classifiers, so no workflow
hardcodes a version. CI.yml writes all of the lines to its job outputs. The
Coverage workflow reads newest-version.

Run it from the repository root: python3 scripts/ci_versions.py
"""

import json
import tomllib

with open("pyproject.toml", "rb") as f:
    classifiers = tomllib.load(f)["project"]["classifiers"]
prefix = "Programming Language :: Python :: 3."
versions = sorted(
    (c.removeprefix("Programming Language :: Python :: ") for c in classifiers if c.startswith(prefix)),
    key=lambda v: tuple(map(int, v.split("."))),
)
assert versions, "no 'Programming Language :: Python :: 3.X' classifiers in pyproject.toml"
# CPython supports free threading from 3.14; PyO3 0.29 dropped 3.13t.
free_threaded = (
    [f"{v}t" for v in versions if tuple(map(int, v.split("."))) >= (3, 14)]
    if any(c.startswith("Programming Language :: Python :: Free Threading") for c in classifiers)
    else []
)
print(f"versions-json={json.dumps(versions)}")
print(f"test-versions-json={json.dumps(versions + free_threaded)}")
# abi3 needs only the oldest GIL interpreter: one wheel covers the rest.
wheel_builds = [{"name": "abi3", "interpreter": versions[0]}]
wheel_builds += [{"name": "cp" + v.replace(".", ""), "interpreter": v} for v in free_threaded]
print(f"wheel-builds-json={json.dumps(wheel_builds)}")
print(f"integration-versions-json={json.dumps([versions[0], versions[-1], *free_threaded[-1:]])}")
print(f"rust-versions-json={json.dumps([versions[-1], *free_threaded[-1:]])}")
print(f"oldest-version={versions[0]}")
print(f"newest-version={versions[-1]}")


def wheel_pattern(version):
    # A GIL build uses the abi3 wheel. A free-threaded build uses its
    # own cp3XX-cp3XXt wheel.
    if version.endswith("t"):
        tag = "cp" + version.removesuffix("t").replace(".", "")
        return f"django_bolt-*-{tag}-{tag}t-*.whl"
    return "django_bolt-*-abi3-*.whl"


print(f"wheel-patterns-json={json.dumps({v: wheel_pattern(v) for v in versions + free_threaded})}")

# Artifact smoke: the abi3 wheel and the sdist install on the oldest
# supported Python, since that is what the abi3 tag and
# requires-python claim. Each free-threaded build gets its own
# cp3XX-cp3XXt wheel, whose pattern is derived from the interpreter.
artifacts = [
    {
        "artifact-kind": "wheel",
        "python-version": v,
        "artifact-pattern": wheel_pattern(v),
    }
    for v in [versions[0], *free_threaded]
]
artifacts.append(
    {
        "artifact-kind": "sdist",
        "python-version": versions[0],
        "artifact-pattern": "django_bolt-*.tar.gz",
    }
)
print(f"artifact-include-json={json.dumps(artifacts)}")

# Django series, same source of truth. Only upstream-supported series
# are listed (see scripts/check_support_matrix.py), and every one of
# them supports every Python above -- so the newest Django runs on all
# Pythons and each older series runs on the oldest Python.
django_prefix = "Framework :: Django :: "
django = sorted(
    (c.removeprefix(django_prefix) for c in classifiers if c.startswith(django_prefix) and c != "Framework :: Django"),
    key=lambda v: tuple(map(int, v.split("."))),
)
assert django, "no 'Framework :: Django :: X.Y' classifiers in pyproject.toml"
print(f"django-newest-json={json.dumps(django[-1:])}")
print("django-include-json=" + json.dumps([{"python-version": versions[0], "django-version": d} for d in django[:-1]]))
