# Mutation testing with PostgreSQL and Redis (django-bolt)

An earlier mutation-testing effort on dj-bolt/django-bolt could not test the code that needs a PostgreSQL or a Redis
server, and the code that only a real `runbolt` process runs. The repository owner asked to set the two services up and
to mutation-test what was left. A coordinating session (id `session_01LwJUCTKW7YUV9kjpjWNenA`) gave you "YOUR TASK"
at the end of your task text.

Read CLAUDE.md of the repository first. All its rules apply: TestClient for in-process tests, a real `runbolt` only for
what TestClient cannot exercise (marker `server_integration`), no mocks of things we own, Red-Green, hot-path rules,
ruff clean, Simplified Technical English in comments, docstrings, commit messages and pull request text. There is no
`gh` and no `just`: use the GitHub MCP tools (`mcp__github__*`, load with ToolSearch) and run justfile commands directly.

## Setup
```
uv sync
uv run maturin develop
```
PostgreSQL 16 and Redis are installed in the container image but not started (check `ls /usr/lib/postgresql/16/bin/initdb`
and `which redis-server`; if one is missing: `apt-get update && apt-get install -y postgresql redis-server`). Start them:
```bash
PGB=/usr/lib/postgresql/16/bin; D=/var/tmp/pgdata
mkdir -p $D && chown postgres:postgres $D && chmod 700 $D
[ -f $D/PG_VERSION ] || runuser -u postgres -- $PGB/initdb -D $D -U postgres --auth=trust -E UTF8 >/tmp/initdb.log 2>&1
runuser -u postgres -- $PGB/pg_ctl -D $D -l /var/tmp/pg.log -o "-p 5432 -c listen_addresses=127.0.0.1 -c max_connections=200" -w start
psql -h 127.0.0.1 -U postgres -c "alter user postgres password 'postgres'"
redis-server --port 6379 --bind 127.0.0.1 --save "" --appendonly no --daemonize yes
export DJANGO_BOLT_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:5432/postgres
export DJANGO_BOLT_TEST_REDIS_URL=redis://127.0.0.1:6379/0
```
Baseline, must pass before you start (the three files are the tests with the markers `postgres` and `redis`; they
start a real `runbolt`):
```
uv run pytest python/tests/integration/test_dead_connection_recovery_server_integration.py python/tests/integration/test_tenant_lanes_server_integration.py python/tests/integration/test_jwt_auth_views_server_integration.py -q -p no:cacheprovider -n 2
uv run pytest python/tests -n 4 -q -p no:cacheprovider
```
The container has no IPv6 (skip the Rust test `bind_listener_binds_an_ipv6_address_in_brackets`).

## How to mutation-test code that only a server process runs
`mutmut` 3 makes a tree `mutants/` with a copy of the package where each function has its mutants and a trampoline. The
trampoline reads the environment variable `MUTANT_UNDER_TEST` at each call: when it holds the name of a mutant of that
function, the mutant runs; else the original runs. mutmut picks the tests for a mutant from an in-process run, so a
function that only the `runbolt` subprocess calls shows as "no tests" and its mutants never run. The server tests start
the subprocess with the environment of the test process, and with the package next to the tests on `PYTHONPATH`
(python/tests/integration/helpers.py, `_process_env`). So you can drive each mutant yourself:

1. Make the work tree (outside the repository), as the script of the earlier effort does:
```bash
root="$(git rev-parse --show-toplevel)"; work=/tmp/mmsvc; rm -rf $work; mkdir -p $work
cp -R "$root/python/django_bolt" "$work/django_bolt"; cp -R "$root/python/tests" "$work/tests"; cp "$root/pytest.ini" "$work/"
# pyproject.toml of the work tree: the one of the repository with this [tool.mutmut] table in place of its own
#   source_paths = ["django_bolt"]
#   only_mutate = ["django_bolt/<file>.py"]            (the file that you test)
#   process_isolation = "forkserver"
#   forkserver_warmup = "none"
#   pytest_add_cli_args_test_selection = [<two or three fast in-process test files that import the file>, "-m", "not postgres", "-p", "no:cacheprovider"]
cd $work && uv run --project "$root" --no-sync --with "mutmut>=3.8,<4" mutmut run --max-children 4 > run.log 2>&1
uv run --project "$root" --no-sync --with "mutmut>=3.8,<4" mutmut results --all true > all.txt
```
   Mutant names are `django_bolt.<module>.x_<function>__mutmut_<n>` and `django_bolt.<module>.xǁ<Class>ǁ<method>__mutmut_<n>`.
   `mutmut show <name>` prints the diff of one mutant.
2. Prove the driver before you trust it. In `$work/mutants`:
   - `MUTANT_UNDER_TEST=none uv run --project "$root" --no-sync pytest <server test file> -q -p no:cacheprovider -x` must PASS
     (the variable must be set: the trampoline raises KeyError when it is missing).
   - Print `django_bolt.__file__` from inside a server subprocess one time (for example a temporary print in a
     copy of a test app) and confirm that it is under `$work/mutants`. If it is the repository copy, find out why
     (an editable install earlier on the path) and correct the `PYTHONPATH` of the run.
   - Pick one mutant that must break a server test (read its diff), run the test with `MUTANT_UNDER_TEST=<its name>`,
     and see the test FAIL. Without these three proofs, a "survived" result means nothing.
3. Drive the mutants of the functions of your task: for each name, run the smallest set of server test files that
   exercises the function, with `MUTANT_UNDER_TEST=<name>`, `-x`, and a timeout (3 times the baseline run time:
   a timeout counts as killed). Exit code 0 = survived; other = killed. Run up to 3 mutants at a time (each test
   takes a free port and its own database; the machine has 4 CPUs). Write `name status seconds` to a result file
   after each mutant so that a stop loses nothing. If the full list would take more than about 2 hours, run the
   functions in the order of your task and say exactly which you ran.

## Classify each survivor (function by function)
- EQUIVALENT: cannot change what a caller or a client observes. COSMETIC: text of a log line or of an exception that
  no client sees and no docs promise. No test for these two.
- GAP: observable behavior that no test asserts. Write a test (see below).
- BUG: the unmutated code is wrong (it contradicts its docstring, docs/src, a sibling function; a client can cause
  a 500; a connection leaks; an option has no effect). Probe the real behavior with a small script against the real
  server and the real service. This is the most valuable result.
- Dead code (no caller in python/django_bolt, python/tests, docs, python/bolt-mcp, python/example): report it.

## Pull requests (you open them; you do NOT merge)
Base `master`, branch from `origin/master`, never stack, never force-push, never touch other pull requests.
- Bug fix: reproduce, ONE failing test first (keep the failing output for the text), smallest correct fix, docs and
  docstrings updated. One pull request for each bug.
- Tests only: one pull request for each source module, at most 6 new test functions. A test that needs PostgreSQL or
  Redis uses the existing fixtures and markers (`postgres_database`, `@pytest.mark.postgres`, `@pytest.mark.redis`,
  `@pytest.mark.server_integration`; read python/tests/integration/conftest.py and the three test files of the
  baseline) so that it skips without the service and runs in CI, which has both services. Prefer an in-process test
  (TestClient, or a direct call of the public function with a real database or cache) when the function can run
  in-process: it is faster and mutmut can use it.
- CHANGELOG.md: do NOT change it. The owner collects the entries in one later pull request. Put the entry in the
  pull request text under a line `CHANGELOG entry (Fixed):`, as `- **<what is true now>** - <what was wrong>. <what happens now>.`
- Before each pull request: `uv run ruff check python/django_bolt <changed test files>`, `uv run ruff format --check <changed python files>`,
  the full suite with the two service variables set, and the three baseline files. Say in the text what you ran.
- Text: follow `.github/PULL_REQUEST_TEMPLATE.md`. For a bug: the reproduction with actual and expected output, the
  test that failed before the fix. For tests only: how many mutants were alive, how many your tests kill, the rest
  in a few lines. Say that mutation testing with a real PostgreSQL or Redis found it. Commit message: conventional
  subject, short body, and the attribution line that your own session instructions give. No Co-Authored-By line.

## Report
When you are done, send ONE message to the coordinating session with the `send_message` tool of the claude-code-remote
MCP server (session_id `session_01LwJUCTKW7YUV9kjpjWNenA`): for each function group: mutants, killed, survived, and
the split of the survivors; each pull request (number, title, one line); bugs found and not fixed, with a
reproduction; dead code; what you did not run and why. Then stop. If the tool is not available, put the report in
your final message.
