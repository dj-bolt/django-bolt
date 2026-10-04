#!/usr/bin/env bash
# Run mutmut (Python mutation testing) on the modules in [tool.mutmut] of
# pyproject.toml.
#
# mutmut looks for the code in ./, src/ or source/. Our package is in python/,
# and src/ holds the Rust crate. Thus this script copies the package and the
# tests to a work directory, in the layout that [tool.mutmut] names, and runs
# mutmut there. The built extension (_core) is copied with the package.
#
# Needs: the extension built into python/django_bolt (`just build`).
# Output: $MUTMUT_DIR (default: mutants-work/), with the mutmut results in
# mutants/ and a summary in summary.txt.
set -euo pipefail

root="$PWD"
work="${MUTMUT_DIR:-mutants-work}"
rm -rf "$work"
mkdir -p "$work"
cp -R python/django_bolt "$work/django_bolt"
cp -R python/tests "$work/tests"
cp pytest.ini pyproject.toml "$work/"

cd "$work"
# On macOS, the Objective-C runtime stops a forked child that initializes a
# class. mutmut forks a child for each mutant.
if [[ "$(uname)" == "Darwin" ]]; then
    export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
fi
# --project: use the environment of the repository, not a new one for the
# copied pyproject.toml. --no-sync: do not rebuild the extension.
mutmut=(uv run --project "$root" --no-sync --with "mutmut>=3.3,<4" mutmut)
"${mutmut[@]}" run --max-children "${MUTMUT_CHILDREN:-$(getconf _NPROCESSORS_ONLN)}"
"${mutmut[@]}" export-cicd-stats
"${mutmut[@]}" results --all false > summary.txt
cat mutants/mutmut-cicd-stats.json
echo
cat summary.txt
