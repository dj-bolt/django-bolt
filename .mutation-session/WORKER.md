# Mutation-testing worker session (django-bolt)

You are one of several parallel sessions that mutation-test the repository dj-bolt/django-bolt and open pull
requests. The repository owner asked for this and decides at merge time; do not stop to ask questions. A
coordinating session (id `session_01LwJUCTKW7YUV9kjpjWNenA`) assigned you the modules in "YOUR TASK" at the end.
Work only on those. You have this machine to yourself (4 CPUs).

Read CLAUDE.md of the repository first. All its rules apply, in particular: TestClient for integration tests, no
mocks of things we own, test behavior through HTTP responses and public API, Red-Green, hot-path rules, ruff clean,
and Simplified Technical English (short sentences, active voice, simple tenses) in docs, comments, docstrings,
CHANGELOG entries, commit messages and pull request text.

## Setup (about 6 minutes)
```
uv sync
uv run maturin develop          # builds the Rust extension; needed once (again only if you change Rust)
uv run pytest python/tests -n 4 -q -p no:cacheprovider   # baseline: must pass before you start
```
Known facts about this kind of container: it has no IPv6, so the Rust unit test
`bind_listener_binds_an_ipv6_address_in_brackets` fails (ignore it; skip it with
`cargo test ... -- --skip bind_listener_binds_an_ipv6_address_in_brackets`). There is no `just` and no `gh`; run
the commands of the justfile directly and use the GitHub MCP tools for pull requests.

## Your own mutation run
Save this script as `/tmp/mm.sh` (outside the repository) and `chmod +x` it:
```bash
#!/usr/bin/env bash
# Usage: mm.sh <source file relative to python/> "<test paths relative to python/>" [mutant name patterns...]
# Runs mutmut on one source file with the named tests. Prints each mutant that is not killed, with its diff.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"; src="$1"; tests="$2"; shift 2
work="$(mktemp -d /tmp/mm-XXXXXX)"; trap 'rm -rf "$work"' EXIT
cp -R "$root/python/django_bolt" "$work/django_bolt"; cp -R "$root/python/tests" "$work/tests"; cp "$root/pytest.ini" "$work/"
SRC="$src" TESTS="$tests" python3 - "$root/pyproject.toml" "$work/pyproject.toml" <<'PY'
import os, re, sys
s = open(sys.argv[1]).read()
head, rest = s.split("[tool.mutmut]", 1)
nxt = re.search(r"^\[", rest, flags=re.M)
tail = rest[nxt.start():] if nxt else ""
args = os.environ["TESTS"].split() + ["-m", "not postgres", "-p", "no:cacheprovider",
    "--deselect=tests/test_nanodjango_plugin.py::TestBoltAPIInit::test_captures_calling_module_name",
    "--deselect=tests/test_slow_user_read_hint.py::test_a_slow_blocking_read_is_reported_one_time_in_dev_mode"]
cfg = ('[tool.mutmut]\nsource_paths = ["django_bolt"]\nonly_mutate = ["%s"]\nprocess_isolation = "forkserver"\n'
       'forkserver_warmup = "none"\npytest_add_cli_args_test_selection = [%s]\n\n') % (
       os.environ["SRC"], ", ".join('"%s"' % a for a in args))
open(sys.argv[2], "w").write(head + cfg + tail)
PY
cd "$work"
mm=(uv run --project "$root" --no-sync --with "mutmut>=3.8,<4" mutmut)
"${mm[@]}" run --max-children "${MM_CHILDREN:-4}" "$@" > run.log 2>&1 || { tr '\r' '\n' < run.log | grep -v 'Running stats$\|Generating mutants$' | tail -30; echo "mutmut failed: no selected test runs the selected mutants, or a selected test fails"; exit 1; }
tr '\r' '\n' < run.log | tail -1
"${mm[@]}" results --all false > results.txt 2>/dev/null || true
grep -v '^\s*$' results.txt | while read -r line; do name="${line%%:*}"; echo "--- $line"
  "${mm[@]}" show "$name" 2>/dev/null | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' || true; done
```
The two deselected tests read the call stack, and mutmut's wrapper functions shift it. Mutant names are
`django_bolt.<module>.x_<function>__mutmut_<n>` and `django_bolt.<module>.xǁ<Class>ǁ<method>__mutmut_<n>`
(separator U+01C1); patterns use fnmatch. Statuses: survived (a gap), no tests (no selected test runs the function),
timeout (the test run hung: counts as killed).
For each source module: pick the test files that exercise it (grep python/tests for its public names), 4 to 8
files, not the very large test_syntax.py and not python/tests/integration; run `mm.sh` with no patterns; save the
output to a file under /tmp. A mutant that survives here may be killed by another test file: before you write a
test for it, grep the tests for that behavior, or re-run that mutant with a wider test list (patterns make it fast).

## Classify each survivor (function by function)
- EQUIVALENT: cannot change what a caller or client observes (a dict key in another letter case for a
  case-insensitive mapping, a default that every caller overrides, a changed argument that the callee ignores,
  two paths with the same result, a capacity hint). No test.
- COSMETIC: text of a log line, warning, repr, or of an exception that never reaches an HTTP client and that no
  documentation promises. No test.
- GAP: observable behavior that no test asserts (status code, response body or header, an error body sent to the
  client, a security check, a limit, a documented default, a branch that picks another result). Write a test.
- BUG: the unmutated code is wrong. This is the most valuable result. Treat each survivor as a hint about behavior
  that nobody checks, read the code around it, and probe the real behavior with small scripts or throwaway tests
  through `TestClient`. Wrong means: it contradicts the docstring, docs/src/, a sibling function, or what the docs
  promise; or a client can cause a 500 with request data; or an accepted option has no effect; or a security check
  can be bypassed.
- Dead code (no caller in python/django_bolt, python/tests, docs/, python/bolt-mcp, python/example): report it, no test.

## Pull requests (you open them yourself)
One pull request per bug fix. One tests-only pull request per source module or small module group. Base `master`.
Create each branch from `origin/master` (`git switch -c <name> origin/master`), never stack branches, never
force-push, never touch branches or pull requests of other sessions, never merge. Do not use `git stash`.

Bug fix (Red-Green):
1. Reproduce with real code. Write ONE failing test first: server behavior that a client sees -> one integration
   test through `TestClient(api)`; a code bug in a function -> one unit test. Put it in the existing test file of
   that area, in the middle of the file next to related tests (other branches append at the end). Keep the failing
   output for the pull request text.
2. Smallest correct fix. No new options unless needed for the fix. Update docstrings and docs/src pages that
   described the old behavior. Never hide errors with a broad `except`.
3. CHANGELOG.md: one entry under `## [Unreleased]` -> `### Fixed`, inserted immediately BEFORE the existing entry
   whose bold title starts with the anchor text listed in YOUR TASK (use the anchors in order, one per pull
   request; this avoids merge conflicts between sessions). Format:
   `- **<what is true now, as a short statement>** - <what was wrong>. <what happens now>.`
4. A behavior change or breaking change goes under a line "**For the merge decision:**" in the pull request text.

Tests-only pull request: at most 6 new test functions per source module, fewer when possible. Prefer one more
`parametrize` case or one more assert in an existing test. Pick the tests that kill the most GAP survivors. No test
that only repeats the implementation, pins a debug text, or tests a private helper that has a public path. Verify
with `mm.sh` and the patterns of the functions you worked on. Never add `# pragma: no mutate`, never change
`[tool.mutmut]` or `.cargo/mutants.toml`.

Before each pull request, on its branch:
```
uv run ruff check python/django_bolt <changed test files>     # must print "All checks passed!"
uv run ruff format --check <changed python files>              # must list no file
uv run pytest python/tests -n 4 -q -p no:cacheprovider         # the full suite must pass; say so in the PR
uv run pytest python/tests/test_docs_snippets.py -q            # when you changed docs
```
Read the output of each command before you commit. Commit subject: conventional (`fix(<area>): ...`,
`test(<area>): ...`); body says what was wrong and what the change does; end the message with the attribution line
that your session's own instructions give you. No Co-Authored-By line, no model names anywhere.

Pull request text: follow `.github/PULL_REQUEST_TEMPLATE.md` (description; "## How was this tested?" as a checklist
that states exactly what you ran and what you did not run; "## Performance consideration"). For a bug: show the
reproduction with actual and expected output, and name the test that failed before the fix. For tests only: say
how many mutants were alive, how many your tests kill, and group the rest (equivalent / cosmetic) in a few lines.
Say that mutation testing found it. End with the session link that your own instructions give you.

## Already open pull requests of this effort (do not duplicate; build on master, not on these)
586 tests ResponseHeaders mapping ops · 587 tests SerializerView only/dump_json/dump_many_json/afrom_model, Serializer.validate ·
588 tests Router.post/put/patch/delete/head/options, ActionHandler · 589 tests WebSocketTestClient.close, shared-connection cursor ·
590 tests Rapidoc/Stoplight docs pages · 591 fix WebSocketTestClient iterators after server close · 592 fix Serializer exclude_unset (drops msgspec.UNSET fields) ·
594 remove unused responses.UploadFile, _json.decode_typed, testing/helpers.py · 595 tests health route paths, JSON decode error line ·
596 tests Router defaults/prefix nesting/middleware chain · 597 tests @action options, urls reverse names, param_functions ·
598 fix Router.include_router(prefix) for earlier routes · 599 fix Router(dependencies=) removed (never ran) · 600 tests auth views ver claim, pk_loader from_db ·
601 tests responses.py (SSE framing with fields, cookie defaults, File media_type) · 602 tests pagination boundary values/list/build_response ·
603 fix pk loader OverflowError · 604 fix Token.decode empty audience · 605 fix format_sse_event bytes with line break ·
606 fix revocation entries live until exp+leeway (stores get leeway=) · 607 fix logout routes reject refresh token with no fam ·
608 fix rotate_refresh_token checks typ · 609 fix JWTAuthViews public_key for asymmetric algorithms · 610 tests middleware_response ·
611 fix pagination cursor URL-safe + wrong-type cursor · 612 fix pagination page/offset beyond data · 613 fix @paginate request lookup
(adds typing.REQUEST_PARAM_NAMES) · 614 fix @paginate sync handler.
Other sessions work in parallel on: dependencies.py, error_handlers.py, views.py, middleware/middleware.py,
auth/backends.py, auth/session.py, auth/guards.py, auth/user_loader.py, and the modules of the other worker sessions.
Known and deliberately not fixed (design questions; do not open pull requests for them): a wrong HTTP method on a
registered path gives 404 not 405; WebSocketClose/WebSocketException are exported but unhandled; Body(embed/alias/
description/example) options have no effect; set_cookie(samesite=<unknown>) becomes Lax.

## Budget and report
Order of work: (1) setup and baseline, (2) mutation runs, (3) BUG probing and fixes, most severe first (security,
then a 500 that a client can cause, then a wrong result, then docs that do not match), (4) tests-only pull
requests. Open at most 8 pull requests; list what you found beyond that. When you are done, send ONE message to the
coordinating session with the `send_message` tool of the claude-code-remote MCP server (load it with ToolSearch if
needed; session_id `session_01LwJUCTKW7YUV9kjpjWNenA`): each pull request (number, title, one line), bugs found
but not fixed with a reproduction, dead-code candidates, and what you did not verify. If that tool is not
available, put the same report in your final message. Then stop.
