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

- When a handler or QuerySet evaluation on the executor pool raises, Bolt runs `close_if_unusable_or_obsolete()` on that thread. A dead connection is dropped and the next request reconnects.
- When a database sets `CONN_MAX_AGE` or `CONN_HEALTH_CHECKS`, Bolt runs the same check before each executor call. Timed recycling and health checks then work as in Django.

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
