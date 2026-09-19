---
icon: lucide/layers
---

# Deployment

This guide explains how to deploy Django-Bolt for production.

## Scaling with processes

Use `--processes` to scale your API. Each process runs a separate Python interpreter, bypassing the GIL limitation:

```bash
# 4 processes = 4 parallel Python executions
python manage.py runbolt --processes 4
```

Under the hood, Django-Bolt uses `SO_REUSEPORT` so the kernel load-balances incoming connections across all processes.

**Rule of thumb:** Set `--processes` to the number of CPU cores available.

## Worker recycling (memory leaks)

Long-running Python processes slowly accumulate resident memory — leaky C extensions, allocator fragmentation, unbounded caches. Traditional servers like Gunicorn work around this with `max_requests` restarts; request count is only a proxy for memory, so Django-Bolt (like Granian) recycles workers on the actual symptoms instead:

```bash
# Recycle any worker whose resident memory exceeds 512 MiB
python manage.py runbolt --processes 4 --max-rss 512

# Also recycle each worker every 6 hours, and replace crashed workers
python manage.py runbolt --processes 4 \
    --max-rss 512 \
    --workers-lifetime 21600 \
    --respawn-failed-workers
```

| Option | Meaning |
| --- | --- |
| `--max-rss <MiB>` | Recycle a worker once its resident set size exceeds this many MiB (checked about once per second). Set it well above your baseline per-worker RSS. `0` disables (default). |
| `--workers-lifetime <seconds>` | Recycle each worker after this much uptime. `0` disables (default). |
| `--respawn-failed-workers` | Fork a replacement when a worker exits unexpectedly (crash, OOM kill) instead of letting the fleet shrink. |
| `--workers-kill-timeout <seconds>` | How long a worker gets to shut down gracefully before it is SIGKILLed (default: 30). |

Any of these options enables the supervising parent process, even with `--processes 1`.

### How recycling works (near-zero-downtime, WebSocket-aware)

Recycling is **spawn-first and graceful** — it is safe for long-lived connections:

1. The supervisor forks a **replacement worker first**; `SO_REUSEPORT` lets old and new workers share the port, so an accepting process is always up. (Closing the old worker's socket can reset the handful of connections already queued on it — negligible with keep-alive clients, which simply retry.)
2. The old worker receives SIGTERM and stops accepting new connections — the kernel routes new traffic to healthy workers.
3. Active **WebSocket connections receive a proper close frame with code `1012` (Service Restart)** instead of an abrupt reset. Clients should treat 1012 as "reconnect now"; the reconnect lands on a healthy worker. New connections to the draining worker are refused, so they retry onto a healthy one.
4. In-flight HTTP requests finish (up to `--workers-kill-timeout`, which also sets the server's internal graceful-shutdown window).
5. If the worker still hasn't exited after the timeout, it is SIGKILLed.

The same drain sequence runs on plain `SIGTERM` (systemd stop, `kubectl delete pod`), so deploys and scale-downs also close WebSockets cleanly. `SIGINT` (Ctrl-C) stops immediately without draining; pressing Ctrl-C twice force-kills workers.

!!! note "WebSocket clients must reconnect"
    No server can migrate a live TCP connection between processes. Recycling closes WebSockets *cleanly* (close code 1012) rather than avoiding the disconnect. Build reconnect logic into your WebSocket clients and keep per-connection session state in Redis/the database so a reconnect is transparent.

The graceful-shutdown window can also be set directly via the `DJANGO_BOLT_SHUTDOWN_TIMEOUT` environment variable (seconds, default 30) when running without the supervisor.

## Production deployment

For production, run Django-Bolt as a managed service behind a reverse proxy.

### Running as a service

You need a process manager to keep Django-Bolt running. Choose systemd (most Linux distributions) or supervisor.

#### With systemd

Create `/etc/systemd/system/django-bolt.service`:

```ini
[Unit]
Description=Django-Bolt API Server
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/path/to/your/project
ExecStart=/path/to/venv/bin/python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable django-bolt
sudo systemctl start django-bolt
```

Check status:

```bash
sudo systemctl status django-bolt
```

#### With supervisor

Install supervisor and create `/etc/supervisor/conf.d/django-bolt.conf`:

```ini
[program:django-bolt]
command=/path/to/venv/bin/python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4
directory=/path/to/your/project
user=www-data
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/django-bolt.log
```

Then reload and start:

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start django-bolt
```

### Reverse proxy with nginx

Once the server is running, configure nginx to proxy requests to it:

```nginx
upstream django_bolt {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name api.example.com;

    location / {
        proxy_pass http://django_bolt;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Test and reload nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Database connections

Django Bolt keeps Django request signals disabled by default for maximum throughput.

```python
# settings.py
BOLT_EMIT_SIGNALS = False
```

With signals disabled, don't rely on request-signal-driven connection recycling.

For ASGI deployments, keep persistent connections disabled (`CONN_MAX_AGE = 0`) and use pooling. See [Persistent connections](https://docs.djangoproject.com/en/6.0/ref/databases/#persistent-connections).

### Option 1: psycopg pool (recommended)

Django 5.1+ has native PostgreSQL connection pooling support with psycopg:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "mydb",
        "USER": "myuser",
        "PASSWORD": "mypassword",
        "HOST": "localhost",
        "CONN_MAX_AGE": 0,  # Required when using Django's psycopg pool
        "OPTIONS": {
            "pool": {
                "min_size": 2,
                "max_size": 10,
            }
        },
    }
}
```

Requires `psycopg[pool]` (`pip install "psycopg[pool]"`). With psycopg2, Django raises `ImproperlyConfigured` if `OPTIONS["pool"]` is set.

### Option 2: PgBouncer (external pooler)

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "mydb",  # Database name configured in PgBouncer
        "HOST": "127.0.0.1",  # PgBouncer host
        "PORT": "6432",
        "CONN_MAX_AGE": 0,  # Recommended for ASGI
        "DISABLE_SERVER_SIDE_CURSORS": True,  # Needed for transaction pooling
    }
}
```

[PgBouncer](https://www.pgbouncer.org/) runs as a separate service and manages connections across all Django processes.

If you need signal compatibility for third-party packages, see [Django Signals](../topics/signals.md).

## Performance tuning

### Socket backlog

For high-traffic servers, increase the socket backlog:

```bash
python manage.py runbolt --processes 4 --backlog 2048
```

### Keep-alive timeout

Adjust HTTP keep-alive timeout (useful for long-lived connections):

```bash
python manage.py runbolt --processes 4 --keep-alive 30
```

### Compression

Enable compression for smaller response sizes:

```python
# settings.py
from django_bolt.middleware import CompressionConfig

BOLT_COMPRESSION = CompressionConfig(
    backend="gzip",
    minimum_size=500,  # Only compress responses > 500 bytes
)
```

## Workers vs Processes

Actix worker threads and Python processes scale different things. `--workers` sets the Actix worker threads in one process. `--processes` sets the number of processes.

On a standard CPython build, Django-Bolt uses 1 worker thread per process. **Python's GIL is the bottleneck**, not Rust. Only one thread can run Python code at a time. More threads only wait:

```
Process with 4 workers:
├── Worker 0: [waiting for GIL]
├── Worker 1: [waiting for GIL]
├── Worker 2: [executing Python handler] ← only one runs
└── Worker 3: [waiting for GIL]
```

The Rust parts (HTTP parsing, routing, compression) take microseconds. Your Python handler takes milliseconds. You will not saturate the Rust side.

**Use processes for parallelism** on a standard build. Each process has its own GIL, so handlers run in parallel.

## Free-threaded Python

Django-Bolt supports the free-threaded build of CPython 3.14 (`python3.14t`, [PEP 703](https://peps.python.org/pep-0703/)). Wheels for `cp314t` are on PyPI. The Rust extension declares that it does not need the GIL, so the interpreter keeps the GIL disabled when it imports `django_bolt`.

With the GIL disabled, `runbolt` defaults `--workers` to the CPU count. Each Actix worker thread runs Python handlers, so one process runs handlers in parallel:

```bash
# One process, one worker thread per CPU
python3.14t manage.py runbolt

# Four processes with two worker threads each
python3.14t manage.py runbolt --processes 4 --workers 2
```

The startup banner shows the mode, for example `Workers: 1 process · 8 threads each · free-threaded`.

What runs in parallel:

- Sync handlers, and async handlers that never await, run on the worker thread that accepted the request. A sync handler that Django-Bolt detects as blocking (ORM access, `time.sleep`, `requests`) runs on the shared executor pool instead, so it does not stall the worker thread.
- Each worker thread owns its own asyncio loop. An async handler runs on the worker thread that accepted the request, and it resumes on that same thread after each await. Handlers on different worker threads run in parallel.
- Auth, guards, CORS, rate limiting, and compression run in Rust, as on every build.

Because each worker thread has its own asyncio loop, loop-bound objects (`asyncio.Lock`, `asyncio.Queue`, futures) cannot be shared between handlers on different worker threads. Treat worker threads like processes for shared asyncio state. With `--workers 1` (the default with the GIL) there is one loop, as before.

Module-level constants that every request reads do not scale with threads on a free-threaded build: the interpreter must synchronize their reference counts across threads. Build the response per request, or split the constant per thread.

Each worker thread holds its own Django database connection, as in any threaded server. Size your connection pool for `processes * workers`. Verify that your own code and third-party packages are thread-safe before you run them with the GIL disabled. Django itself does not yet declare free-threading support.

To keep the GIL on a free-threaded interpreter, start Python with `-X gil=1` or set `PYTHON_GIL=1`. `runbolt` then uses 1 worker thread again.
