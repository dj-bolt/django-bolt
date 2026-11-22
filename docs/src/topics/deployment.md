---
description: Run Django-Bolt in production with runbolt, systemd or supervisor, nginx or Caddy, a database pool, Docker and free-threaded Python.
---

# Deployment

`runbolt` is the production server of Django-Bolt. You do not need Gunicorn or Uvicorn.
Put a process manager around it and a reverse proxy in front of it.

```bash
python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4
```

## Production checklist

Do these steps before the first deploy:

1. Set `DEBUG = False`. Read `SECRET_KEY` from the environment. Set `ALLOWED_HOSTS`.
2. Run `python manage.py check --deploy`, and fix each warning that applies to you.
3. Run `python manage.py migrate`. `runbolt` warns about unapplied migrations, but it does not stop.
4. Run `python manage.py collectstatic`. With `DEBUG = False`, Bolt does not search the static files of apps, such as the admin. See [Static files](static-files.md).
5. Bind `runbolt` to `127.0.0.1`, and put [a reverse proxy](#run-behind-a-reverse-proxy) in front of it.
6. Add the proxy to [`BOLT_TRUSTED_PROXIES`](../ref/settings.md#bolt_trusted_proxies).
7. Set `--processes` to the number of CPU cores. See [Choose processes and workers](#choose-processes-and-workers).
8. Run `runbolt` under [systemd or supervisor](#run-as-a-service), with a stop timeout longer than `--workers-kill-timeout`.
9. Use [a connection pool or PgBouncer](#configure-database-connections) for PostgreSQL.
10. Define `LOGGING` in your settings. See [Logging](logging.md).
11. Add [health checks](health-checks.md) for the load balancer.
12. Do not use `--dev` in production.

## Choose processes and workers

`--processes` sets the number of processes. `--workers` sets the number of worker threads in each process.
Each worker thread runs Python handlers.

On a standard CPython build, the GIL lets one thread run Python at a time.
More threads in one process only wait, so the default is 1 worker.
Use processes for parallel work. Start with one process for each CPU core:

```bash
python manage.py runbolt --host 127.0.0.1 --processes 8
```

With more than 1 process, `runbolt` becomes a supervisor. It forks the workers, and each worker binds the same port with `SO_REUSEPORT`.
On Linux, the kernel spreads the connections across the workers.
On macOS, the kernel sends each connection to one process, so more processes add no throughput there.

Each process loads your project and holds its own database connections.
Measure memory and database connections when you add processes.

In a container, `os.cpu_count()` gives the CPUs of the host, not the CPU limit of the container.
Set `--processes` and `--workers` to fit the limit.

For a free-threaded Python, see [Run on free-threaded Python](#run-on-free-threaded-python).

## Recycle workers

A Python process can grow in memory over days, for example from a leak in a C extension.
`runbolt` can replace a worker when it is too large or too old:

```bash
python manage.py runbolt --processes 4 \
    --max-rss 512 \
    --workers-lifetime 21600 \
    --respawn-failed-workers
```

- `--max-rss 512` replaces a worker when its resident memory is more than 512 MiB. Set it well above the normal size of a worker.
- `--workers-lifetime 21600` replaces each worker after 6 hours.
- `--respawn-failed-workers` replaces a worker that crashes or that the OOM killer stops.

The supervisor starts the new worker first. The old worker then drains: it finishes its requests, and it closes each WebSocket with code `1012` (Service Restart).
The port always has a worker that accepts connections.
These options start the supervisor also with `--processes 1`.
See [runbolt](../ref/runbolt.md#worker-recycling).

A WebSocket client must connect again after code `1012`. Keep the state of a connection in the database or in a cache, not in the worker.

## Run as a service

`runbolt` exits with status 1 when it cannot start, for example on a port in use or a failed system check.
It exits with status 0 after `SIGTERM`. Configure the process manager to restart on failure.

### systemd

Create `/etc/systemd/system/blog.service`:

```ini
[Unit]
Description=Blog API (Django-Bolt)
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/srv/blog
Environment=DJANGO_SETTINGS_MODULE=blog.settings
ExecStart=/srv/blog/.venv/bin/python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4
Restart=on-failure
RestartSec=3
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
```

`systemctl stop` sends `SIGTERM` to each process of the service. `TimeoutStopSec=40` gives the workers their 30 seconds of drain.
Do not add an `ExecReload=` line that sends `SIGHUP`. `runbolt` has no reload signal.

Start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now blog
sudo systemctl status blog
```

To deploy new code, run `sudo systemctl restart blog`.

### supervisor

Create `/etc/supervisor/conf.d/blog.conf`:

```ini
[program:blog]
command=/srv/blog/.venv/bin/python manage.py runbolt --host 127.0.0.1 --port 8000 --processes 4
directory=/srv/blog
environment=DJANGO_SETTINGS_MODULE="blog.settings"
user=www-data
autostart=true
autorestart=unexpected
exitcodes=0
stopsignal=TERM
stopwaitsecs=40
killasgroup=true
redirect_stderr=true
stdout_logfile=/var/log/blog/runbolt.log
```

supervisor sends `SIGTERM` to the `runbolt` supervisor, which stops its workers.
`stopwaitsecs=40` waits for the drain. `killasgroup=true` also sends `SIGKILL` to the workers after that time.

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status blog
```

## Run behind a reverse proxy

`runbolt` does not end TLS. It has no timeout for a slow client that sends its request headers.
Put a reverse proxy in front of it for TLS, for request timeouts, and for the static files.

### nginx

```nginx
upstream blog {
    server 127.0.0.1:8000;
    keepalive 32;
}

map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      "";
}

server {
    listen 443 ssl;
    server_name api.example.com;
    # ssl_certificate and ssl_certificate_key go here.

    location /static/ {
        alias /srv/blog/staticfiles/;
        expires 1y;
    }

    location / {
        proxy_pass http://blog;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

`expires 1y` is safe only for file names that contain a hash of the content. See [Static files](static-files.md).
The `Upgrade` and `Connection` headers let WebSocket requests through. The `map` sends an empty `Connection` header for other requests, so nginx keeps its upstream connections open.
Test and reload nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

### Caddy

```text
api.example.com {
    handle_path /static/* {
        root * /srv/blog/staticfiles
        file_server
    }
    handle {
        reverse_proxy 127.0.0.1:8000
    }
}
```

Caddy gets a TLS certificate, and it sets `X-Forwarded-For` and `X-Forwarded-Proto`. It also passes WebSocket upgrades.

### Trust the proxy headers

Bolt reads `X-Forwarded-For` and `X-Forwarded-Proto` only from a peer in `BOLT_TRUSTED_PROXIES`.
A client can send these headers itself, so Bolt ignores them from every other peer.
Add the address of the proxy:

```python title="blog/settings.py"
BOLT_TRUSTED_PROXIES = ["127.0.0.1"]
```

This handler shows what Bolt sees:

```python title="blog/api.py"
from django_bolt import BoltAPI, Request

api = BoltAPI()


@api.get("/whoami")
async def whoami(request: Request) -> dict:
    return {
        "client": request.META["REMOTE_ADDR"],
        "scheme": request.scheme,
        "secure": request.is_secure(),
    }
```

```bash
curl -H "X-Forwarded-For: 203.0.113.7" -H "X-Forwarded-Proto: https" localhost:8000/whoami
```

Without the setting:

```json
{"client": "127.0.0.1", "scheme": "http", "secure": false}
```

With `BOLT_TRUSTED_PROXIES = ["127.0.0.1"]`:

```json
{"client": "203.0.113.7", "scheme": "https", "secure": true}
```

Rate limits with `key="ip"` and the scheme of a mounted Django app, such as the admin, use the same values.
Without this setting, all clients behind the proxy share one rate-limit bucket.

You do not need Django's `SECURE_PROXY_SSL_HEADER`.
That setting trusts the header from every peer. If you set it, make sure that the proxy replaces the header of the client, as `proxy_set_header` does.

## Configure database connections

Bolt does not close database connections at the end of each request.
Each thread that runs a query keeps its connection for the next request, also with `CONN_MAX_AGE = 0`.
These threads can hold a connection:

- the worker threads (`--workers`),
- the ORM pool (`DJANGO_BOLT_ORM_THREADS`, 4 by default for PostgreSQL),
- the executor pool for blocking sync handlers (`DJANGO_BOLT_EXECUTOR_THREADS`, CPU count + 4, at most 32),
- one request lane for each request in flight on a route with Django middleware.

Multiply by `--processes`. Make sure that the database accepts that number of connections, or use a pool.

When a query fails on a dead connection, Bolt drops that connection, and the other threads connect again before their next call.
Thus a database restart fails the requests that meet the first dead connection. To check each connection before use, set `CONN_HEALTH_CHECKS = True` on a database without a pool.

See [Async ORM](async-orm.md) and [Signals](signals.md) for the details.

### Use the psycopg pool

Install the `pool` extra. It installs psycopg with `psycopg_pool` 3.3 or later:

```bash
pip install "django-bolt[pool]"
```

psycopg also needs libpq. Install it on the host, or install `psycopg[binary]`.
Then turn on the pool of Django:

```python title="blog/settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "blog",
        "USER": "blog",
        "HOST": "127.0.0.1",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "pool": {"min_size": 2, "max_size": 10},
        },
    }
}
```

With a pool, a thread holds a connection only while it has work. Thus `max_size` limits the queries in flight in one process, not the threads.
A call that waits longer than the `timeout` of the pool (30 seconds by default) fails.
Django requires `CONN_MAX_AGE = 0` with a pool.

Do not set `CONN_HEALTH_CHECKS` or a pool `check` for a pooled database. Bolt then logs a warning at startup, because each check adds one round trip.
After a database restart, Bolt drains the pool when it finds the first dead connection.

`runbolt --processes N` closes the pools of the supervisor before it forks. Each worker opens its own pool.

### Use PgBouncer

[PgBouncer](https://www.pgbouncer.org/) shares a small number of connections across all processes and hosts.
For transaction pooling, turn off server-side cursors:

```python title="blog/settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "blog",
        "HOST": "127.0.0.1",
        "PORT": "6432",
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
}
```

### Set network timeouts

During a network partition, a query gets no answer and no error. With the Linux defaults, the request waits 15 minutes or more.
Give libpq TCP timeouts:

```python title="blog/settings.py"
DATABASES["default"]["OPTIONS"] |= {
    "connect_timeout": 5,
    "keepalives_idle": 5,
    "keepalives_interval": 2,
    "keepalives_count": 3,
    "tcp_user_timeout": 10000,
}
```

With these values, a connection with no answer fails after about 10 seconds. Bolt then drops it.
See [libpq connection parameters](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-PARAMKEYWORDS).

## Run in Docker

```dockerfile title="Dockerfile"
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/blog
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["python", "manage.py", "runbolt", "--host", "0.0.0.0", "--port", "8000", "--processes", "4"]
```

Use the exec form of `CMD`, as above. `runbolt` is then process 1 and gets the `SIGTERM` of `docker stop`.
In a container, bind `0.0.0.0` so that the port mapping reaches the server.

`docker stop` waits 10 seconds by default before `SIGKILL`. Give the workers time to drain:

```yaml title="compose.yaml"
services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    stop_grace_period: 35s
```

In Kubernetes, set `terminationGracePeriodSeconds` above `--workers-kill-timeout`. Both are 30 seconds by default.

## Run on free-threaded Python

Bolt supports the free-threaded build of CPython, for example `python3.14t`. See [Installation](../intro/install.md) for the versions.
With the GIL disabled, each worker thread runs Python in parallel.
`runbolt` then sets `--workers` to the CPU count by default:

```bash
python3.14t manage.py runbolt --host 127.0.0.1
```

The banner shows the mode, for example `Workers  1 process · 8 threads each · free-threaded`.

On a free-threaded build, one process can replace several processes:

- Each worker thread has its own asyncio loop. Do not share an `asyncio.Lock`, an `asyncio.Queue` or a future between handlers on different threads.
- Each worker thread and each pool thread holds its own database connection.
- Make sure that your code and your packages are safe for threads.
- Increase `DJANGO_BOLT_ORM_THREADS`. Its default is sized for the GIL. Start with one ORM thread for each one or two worker threads, then measure.

```bash
DJANGO_BOLT_ORM_THREADS=8 python3.14t manage.py runbolt --host 127.0.0.1 --workers 12
```

Do not increase `DJANGO_BOLT_ORM_THREADS` on a build with the GIL and SQLite. There, more SQLite connections decrease throughput.
To keep the GIL on a free-threaded interpreter, start Python with `-X gil=1` or set `PYTHON_GIL=1`. `runbolt` then uses 1 worker again.
For measured numbers, see [Benchmarks](../benchmarks.md).

## Stop the server gracefully

`SIGTERM` drains each worker:

1. The worker stops accepting connections.
2. It closes each WebSocket with code `1012`.
3. It finishes its requests in flight, for up to `--workers-kill-timeout` seconds (30 by default).
4. The supervisor sends `SIGKILL` to a worker that still runs after that time.

A request in flight completes, and `runbolt` exits with status 0 after the last response.
`SIGINT` (Ctrl+C) stops one process at once, and the request in flight fails.
Give the process manager a stop timeout longer than `--workers-kill-timeout`.
For each signal, see [runbolt](../ref/runbolt.md#signals).

!!! warning "Do not send SIGHUP"
    `runbolt` has no handler for `SIGHUP`. It stops one process at once. Under a supervisor, it stops only the supervisor, and the workers continue with the old code.

## See also

- [runbolt reference](../ref/runbolt.md)
- [Settings reference](../ref/settings.md)
- [Logging](logging.md)
- [Health checks](health-checks.md)
- [Static files](static-files.md)
- [Media files](media-files.md)
