---
icon: lucide/radio
---

# Django Signals

Django Bolt supports optional Django signal emission for compatibility with Django ecosystem features that depend on `request_started` and `request_finished` signals.

For more information on Django signals, see the [Django Signals documentation](https://docs.djangoproject.com/en/5.1/topics/signals/).

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

- Always: a handler, a QuerySet evaluation, or the load of `request.user` on the executor pool can raise. Bolt then runs `close_if_unusable_or_obsolete()` on that thread. A dead connection is dropped and the next request reconnects. The request that met the dead connection fails: the connection looks fine until it is used.
- With a positive `CONN_MAX_AGE` or with `CONN_HEALTH_CHECKS`: Bolt runs the same check before each executor call too. Timed recycling and health checks then work as in Django. A health check finds a dead connection before the query and replaces it, so no request fails.

Neither setting is on by default. A project that sets neither gets the first tier only. After a database failover or a restart of PostgreSQL, one request on each executor thread fails. The ones after it serve. Set `CONN_HEALTH_CHECKS = True` with a positive `CONN_MAX_AGE` to serve them all. `CONN_MAX_AGE` must be positive for that. With the default of `0`, Django closes the connection as obsolete on the same check, before every call.

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
