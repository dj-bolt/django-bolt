---
description: Every option, environment variable, exit status and signal of the runbolt management command that serves a Django-Bolt project.
---

# runbolt

`runbolt` is the Django management command that serves your Bolt APIs.
It is the development server and the production server.

```bash
python manage.py runbolt [--host HOST] [--port PORT] [--processes N] [--workers N]
                         [--dev] [--reload-dir PATH] [--no-admin] [--skip-checks]
                         [--backlog N] [--keep-alive SECONDS]
                         [--max-rss MIB] [--workers-lifetime SECONDS]
                         [--respawn-failed-workers] [--workers-kill-timeout SECONDS]
```

For a guide to production use, see [Deployment](../topics/deployment.md).

## Options

| Option | Type | Default | Environment variable |
| --- | --- | --- | --- |
| `--host` | str | `0.0.0.0` | none |
| `--port` | int, 0 to 65535 | `8000` | none |
| `--processes` | int, 1 or more | `1` | none |
| `--workers` | int, 1 to 512 | `1`, or the CPU count on a free-threaded Python | `DJANGO_BOLT_WORKERS` (set by `runbolt`) |
| `--dev` | flag | off | none |
| `--reload-dir` | path, repeatable | none | none |
| `--no-admin` | flag | off | none |
| `--skip-checks` | flag | off | none |
| `--backlog` | int, 0 or more | `1024` | `DJANGO_BOLT_BACKLOG` (set by `runbolt`) |
| `--keep-alive` | int, 0 or more | no timeout | `DJANGO_BOLT_KEEP_ALIVE` |
| `--max-rss` | int, 0 or more | `0` (off) | none |
| `--workers-lifetime` | int, 0 or more | `0` (off) | none |
| `--respawn-failed-workers` | flag | off | none |
| `--workers-kill-timeout` | int, 0 or more | `30` | `DJANGO_BOLT_SHUTDOWN_TIMEOUT` |

A number out of range stops `runbolt` before startup. The error names the option:

```text
CommandError: --workers must be at least 1, got 0
```

`runbolt` also accepts the common options of each Django command: `--settings`, `--pythonpath`, `--traceback`, `--no-color`, `--force-color`, `-v` and `--version`.
`--version` prints the Django version.

### --host

The address to bind. The value is an IP address, an IPv6 address in brackets such as `[::1]`, or a host name.
A host name binds its first IPv4 address, as `runserver` does. Thus `localhost` binds `127.0.0.1`.
A host name that does not resolve stops startup.

The default `0.0.0.0` accepts connections on each IPv4 interface.
Behind a reverse proxy on the same machine, bind `127.0.0.1`.

### --port

The TCP port to bind.

### --processes

The number of worker processes.
With more than 1, the `runbolt` process becomes a supervisor. It forks one worker for each process.
Each worker binds the same port with `SO_REUSEPORT`, and the kernel spreads the connections across them.

This option needs `os.fork()`. On Windows, a value above 1 stops startup with `CommandError`.
On macOS, the kernel sends each connection to one process, so more processes add no throughput there.

### --workers

The number of worker threads in each process. Each thread runs Python handlers and has its own asyncio loop.

With the GIL, only one thread runs Python at a time, so the default is 1.
On a free-threaded Python with the GIL disabled, the default is `os.cpu_count()`.
`runbolt` writes the value to `DJANGO_BOLT_WORKERS` before the server starts. A value that you set in the environment has no effect.

### --dev

Turn on automatic reload. See [Development mode](#development-mode).

### --reload-dir

Watch one more directory or file in development mode. Give the option one time for each path.
A relative path starts at the working directory. A path that does not exist prints a warning and is not watched.
Without `--dev`, `runbolt` prints `Warning: --reload-dir is ignored without --dev`.

### --no-admin

Do not serve the Django admin.
By default, `runbolt` mounts the admin at the prefix of `admin.site.urls` in `ROOT_URLCONF`, when `django.contrib.admin` is installed.

### --skip-checks

Skip the Django system checks at startup. The check for unapplied migrations still runs.

### --backlog

The size of the listen queue of the socket: the connections that the kernel holds before the server accepts them.
The kernel limit `net.core.somaxconn` can make the real queue smaller.

### --keep-alive

The time in seconds that an idle HTTP/1.1 connection stays open.
`0` closes each connection after its response.
Without the option, `runbolt` reads `DJANGO_BOLT_KEEP_ALIVE`. Without both, the server sets no timeout, and an idle connection stays open until the client or the OS closes it.

### --max-rss

Recycle a worker when its resident memory is more than this number of MiB. `0` turns the check off.
The supervisor reads the memory of each worker about one time each second.
See [Worker recycling](#worker-recycling).

### --workers-lifetime

Recycle each worker after it ran for this number of seconds. `0` turns the limit off.

### --respawn-failed-workers

Start a new worker when a worker stops without a request from the supervisor, for example after a crash or an OOM kill.
Without this option, the supervisor does not replace the worker, and the number of workers goes down.

A worker that stops before it listens on the port is a startup failure, for example a port in use or an import error.
A new worker fails in the same way. Thus the supervisor stops all workers, and `runbolt` exits with status 1.

### --workers-kill-timeout

The time in seconds that a worker gets to stop after `SIGTERM`.
In that time, the worker finishes its requests in flight and closes its WebSocket connections.
After it, the server closes the connections that are still open, and the supervisor sends `SIGKILL`.

`runbolt` copies the value to `DJANGO_BOLT_SHUTDOWN_TIMEOUT`, the drain time of the server.
A value of `DJANGO_BOLT_SHUTDOWN_TIMEOUT` that you set yourself wins for the drain time.
The supervisor always uses the option for its `SIGKILL` deadline.

## Startup

`runbolt` does these steps, in this order:

1. Run the Django system checks, unless you give `--skip-checks`. An error stops startup.
2. Run the migration check. Unapplied migrations print a warning. They do not stop startup.
3. Find the `BoltAPI` instances. `BOLT_API` lists them. Without the setting, `runbolt` looks in the `api.py` and `bolt_api.py` modules of the project and of each installed app. See [Routing](../topics/routing.md).
4. Merge the APIs. Two APIs with the same method and path stop startup with `Route conflict: GET /articles defined in both ...`.
5. Add the OpenAPI routes and the admin routes.
6. Bind the port and start the server.

In development mode, each reload runs these steps again.
With `--processes` above 1, steps 1 to 5 run one time in the supervisor. Each worker then runs steps 3 to 6.

`runbolt` prints a banner when the server is ready:

```text
  ⚡ Django Bolt v0.13.0  ready in 51 ms

  ➜  Local: http://0.0.0.0:8000
  ➜  Docs:  http://0.0.0.0:8000/docs
  ➜  Admin: http://0.0.0.0:8000/admin/

  Mode           production
  Routes         3 API · 1 ASGI · +8 framework · 1 app
  Workers        4 processes
  Compression    enabled
  Middleware     11 handlers
  Recycling      max-rss 512MiB

  press CTRL+C to stop
```

The banner uses ASCII characters when the output encoding has no Unicode support.
`--no-color` or the `NO_COLOR` environment variable removes the colors.

## Exit status

| Status | Cause |
| --- | --- |
| `0` | The server stopped after `SIGTERM` or `SIGINT`. |
| `1` | An option is out of range. |
| `1` | A system check failed. |
| `1` | No `BoltAPI` was found: `No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment, e.g., api = BoltAPI()`. |
| `1` | A `BOLT_API` entry did not import, or two APIs have the same route. |
| `1` | The port is in use: `RuntimeError: Server error: Address already in use (os error 98)`. |
| `1` | With a supervisor: each worker stopped without a shutdown request, and at least one failed. |
| `1` | With `--respawn-failed-workers`: a worker failed to start. |
| other | In development mode: the first start of the server failed with this status. |

A process manager uses the status. For example, systemd restarts the service with `Restart=on-failure` after status 1, and not after status 0.

## Signals

| Signal | One process | Supervisor (`--processes` above 1, or a recycling option) |
| --- | --- | --- |
| `SIGTERM` | Drain, then stop. | Send `SIGTERM` to each worker. Each worker drains. `SIGKILL` after `--workers-kill-timeout`. |
| `SIGINT` (Ctrl+C) | Stop at once. Requests in flight fail. | Same as `SIGTERM`. A second `SIGINT` sends `SIGKILL` to each worker that still runs. |
| `SIGQUIT` | Stop at once. | Same as `SIGTERM`. |
| `SIGHUP` | No handler: the process stops at once. | Same as `SIGTERM`. |

To drain, a worker does these steps:

1. Stop accepting new connections.
2. Close each WebSocket connection with close code `1012` (Service Restart). New WebSocket upgrades get `503`.
3. Finish the requests in flight, for up to `--workers-kill-timeout` seconds. Idle keep-alive connections close at once.
4. Close the database connections of its request lanes, and exit.

In a terminal, Ctrl+C sends `SIGINT` to the supervisor and to each worker, so the workers stop at once.

!!! warning "runbolt does not reload on SIGHUP"
    `runbolt` has no reload signal. Do not send `SIGHUP` to reload code. To deploy new code, restart the service with `SIGTERM`.
    `SIGHUP` stops `runbolt`. With a supervisor, the workers drain as on `SIGTERM`, and `runbolt` exits with status 0.

## Worker recycling

`--max-rss`, `--workers-lifetime` and `--respawn-failed-workers` start the supervisor, also with `--processes 1`.
`--workers-kill-timeout` alone does not start it.

To recycle a worker, the supervisor does these steps:

1. Fork the new worker. The new worker binds the port with `SO_REUSEPORT` while the old worker still serves.
2. Send `SIGTERM` to the old worker. The old worker drains as in [Signals](#signals).
3. Send `SIGKILL` to the old worker if it still runs after `--workers-kill-timeout` seconds.

The supervisor recycles not more than one worker each second. Thus all workers do not restart at the same time.
It writes one line for each event:

```text
  20:00:39 [bolt] Recycling worker 8588 (slot 0): lifetime 3s reached
  20:08:10 [bolt] Worker 14735 (slot 0) exited unexpectedly (code -9); respawning
  20:08:12 [bolt] Shutting down 2 workers...
```

## Development mode

`--dev` runs the server in a child process and starts it again when a file changes.

- A change to a `.py` or `.html` file reloads the server. Other files do not.
- The watcher watches the project root (`BASE_DIR`, or the working directory) recursively. It also watches each module that the project imports, the template directories, `manage.py`, and each `--reload-dir`.
- The watcher ignores the virtual environment and these directories: `.git`, `.hg`, `.svn`, `.idea`, `.tox`, `.venv`, `.vscode`, `.nox`, `.cache`, `venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `build`, `dist`, `node_modules` and `target`. It also ignores editor temporary files, for example `*.swp` and `*~`.
- Changes in a period of 50 ms give one reload.
- `--dev` forces `--processes 1`, and it ignores the recycling options. It prints a warning for each.
- The banner shows `127.0.0.1` in place of `0.0.0.0`. The server still binds the address of `--host`.
- `--dev` does not change `DEBUG`.

A reload stops the old server with `SIGTERM`, and with `SIGKILL` after 100 ms. It does not drain.
The console shows the file that changed:

```text
  20:03:39 [bolt] reloading blog/api.py
System check identified no issues (0 silenced).
  ✓ Reloaded in 456 ms  http://127.0.0.1:8000
```

When the first start fails, for example because the port is in use, `runbolt` exits with the status of the server.
When the server fails after a reload, for example on a syntax error, `runbolt` waits for the next change.

`SIGINT`, `SIGTERM` and `SIGHUP` stop the server and then `runbolt`. When you start `runbolt --dev` under `nohup`, `SIGHUP` stays ignored.

Set `BOLT_DEV_FORCE_POLLING = True` when file events do not arrive, for example in a Docker container on a Windows host. The watcher then reads the files every 500 ms.

## Environment variables that runbolt sets

`runbolt` sets these variables in each worker process. Your code can read them.

| Variable | Value |
| --- | --- |
| `DJANGO_BOLT_PROCESS_ID` | The slot of the worker, from `0` to `--processes` minus 1. Set only under a supervisor. A recycled worker keeps the slot. |
| `DJANGO_BOLT_REUSE_PORT` | `1` under a supervisor. Set it yourself to bind with `SO_REUSEPORT` in one process. |
| `DJANGO_BOLT_WORKERS` | The value of `--workers`. |
| `DJANGO_BOLT_BACKLOG` | The value of `--backlog`. |

!!! note "Two supervisors can share one port"
    Each worker of a supervisor binds with `SO_REUSEPORT`. A second `runbolt --processes 2` on the same port and as the same user also starts, and the kernel splits the connections between the two.

For the other `DJANGO_BOLT_*` variables, see [Settings](settings.md#environment-variables).

## Examples

```bash
# Development: reload on each change
python manage.py runbolt --dev

# Watch a shared library outside the project
python manage.py runbolt --dev --reload-dir ../shared-lib

# Production behind a proxy on the same machine
python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4

# Recycle a worker above 512 MiB, and replace a worker that crashes
python manage.py runbolt --processes 4 --max-rss 512 --respawn-failed-workers
```

## See also

- [Deployment](../topics/deployment.md)
- [Settings](settings.md)
- [Logging](../topics/logging.md)
