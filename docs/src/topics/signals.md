---
icon: lucide/radio
---

# Django Signals

Django Bolt supports optional Django signal emission for compatibility with Django ecosystem features that depend on `request_started` and `request_finished` signals.

For more information on Django signals, see the [Django Signals documentation](https://docs.djangoproject.com/en/stable/topics/signals/).

## Why Signals Are Optional

Django Bolt is designed for maximum performance. Django's signal system adds overhead to every request, which matters for high-throughput APIs. Django Bolt disables signals by default to eliminate this overhead.

## Enabling Signals

Add to your Django settings:

```python
# settings.py
BOLT_EMIT_SIGNALS = True
```

## When You Need Signals

### Database Connection Management

Django Bolt keeps database connections open across requests. Bolt does not need the request signals to manage them:

- Always: a handler, a QuerySet evaluation, or the load of `request.user` on the executor pool can raise. Bolt then runs `close_if_unusable_or_obsolete()` on that thread. Bolt drops the dead connection, and the next request reconnects. The request that met the dead connection fails: the connection looks fine until it is used. Bolt skips a connection inside an open `atomic` block. The next check after the block ends drops it.
- Always: a database restart or failover kills the connection of each thread at once. When Bolt drops a connection that does not answer, every thread closes its older connections to that database before its next call, and reconnects. Connections to other databases stay open. A request lane does this when its request starts. Thus the first request that meets a dead connection fails, and so can requests that run on other threads at that time. The requests after it serve, on every thread. One restart makes one reconnect: a thread that meets a dead connection from before the first reconnect closes only its own. A query that fails on a connection that still answers, for example on a missing table, closes nothing on the other threads.
- With a positive `CONN_MAX_AGE` or with `CONN_HEALTH_CHECKS`: Bolt runs the same check before each executor call too. Timed recycling and health checks then work as in Django. A health check finds a dead connection before the query and replaces it, so no request fails.

Neither setting is on by default. A project that sets neither gets the first two tiers. After a database failover or a restart of PostgreSQL, the first request that meets a dead connection fails. Set `CONN_HEALTH_CHECKS = True` to serve that one too. With the default `CONN_MAX_AGE` of `0`, the same check closes the connection as obsolete before every call, so each call reconnects. Set a positive `CONN_MAX_AGE` as well to keep connections between calls.

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "mydb",
        "CONN_MAX_AGE": 600,  # Recycle connections older than 600s
        "CONN_HEALTH_CHECKS": True,  # Ping before the first query of a request
    }
}
```

The check before each call costs about 2 µs per open connection. Leave both settings unset to skip it. For PostgreSQL, a connection pool is the better option. See [Database connections](../getting-started/deployment.md#database-connections) in the deployment guide.

With Django's connection pool (`OPTIONS["pool"]`), `CONN_MAX_AGE` must be `0`. A thread of Bolt then holds a pooled connection only while it has work. A worker of a Bolt thread pool gives the connection back when its queue is empty. A request lane gives it back at the end of each request, and a sync streaming body when the body ends. A call on another thread, such as the event loop, gives it back when the call ends. A thread that kept its connection would hold a pool slot while it is idle. A thread that your code starts keeps its connection until it closes the connection. The check before each call skips a pooled database: the pool checks its own connections.

Leave `CONN_HEALTH_CHECKS` off for a pooled database, and do not set `check` in `OPTIONS["pool"]`. Either one makes the pool run a check on each checkout. A worker checks out a connection again after each idle time, so many ORM calls get one more round trip. Bolt logs a warning at startup when a pool has a check.

Bolt also keeps the pool clean after a database restart. The restart killed the idle connections of the pool too. Without a check, the pool gives each dead idle connection to a request, and that request fails. With a check, the pool tests idle connections one at a time, and it sleeps after each one that fails: 0, 1, 2, 4 s and so on. Thus, when Bolt finds the first dead connection of a pool, it runs `drain()` of that pool. The pool closes its idle connections and opens new ones. It also drops each older connection that a thread gives back. `drain()` sends nothing on the network, so a network partition cannot make the thread wait.

`drain()` is in psycopg_pool 3.3 and later. The `pool` extra installs it: `pip install "django-bolt[pool]"`. With an older psycopg_pool, Bolt logs a warning at startup and does not drain the pool. After a restart, each dead idle connection of the pool then fails one request.

A forked process inherits the open connections of its parent. Two processes that use one connection read the results of each other's queries. `runbolt --processes N` closes the connections and the pools of its parent before it starts each worker.

### Third-Party Packages

Some Django packages rely on signals:

- **django-debug-toolbar** - Uses signals for request tracking
- **django-silk** - Profiling middleware uses signals
- **Custom audit logging** - May hook into request signals

If you use such packages, enable signals:

```python
BOLT_EMIT_SIGNALS = True
```

## Summary

| Setting | Performance | Use Case |
|---------|-------------|----------|
| `BOLT_EMIT_SIGNALS=False` (default) | Maximum | Most APIs with connection pooling |
| `BOLT_EMIT_SIGNALS=True` | Slight overhead | Debug tools, signal receivers |

**Rule of thumb:** Use connection pooling and keep signals disabled unless you have a specific reason to enable them.
