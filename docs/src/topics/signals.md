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

Django Bolt disables signals by default, which means Django's automatic connection cleanup doesn't run. For async applications, Django [recommends using connection pooling](https://docs.djangoproject.com/en/5.1/ref/databases/#persistent-connections) instead of relying on signals.

**Recommended: Use connection pooling (no signals needed)**

See [Database connections](../getting-started/deployment.md#database-connections) in the deployment guide for setup instructions.

**Alternative: Enable signals for `CONN_MAX_AGE`**

If you need Django's timed connection recycling:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "mydb",
        "CONN_MAX_AGE": 600,  # Close connections after 600s idle
        "CONN_HEALTH_CHECKS": True,  # Verify connections before use
    }
}
BOLT_EMIT_SIGNALS = True  # Required for CONN_MAX_AGE to work!
```

Django's `request_finished` signal triggers `close_old_connections()` which checks `CONN_MAX_AGE` and closes stale connections.

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
| `BOLT_EMIT_SIGNALS=True` | Slight overhead | Need `CONN_MAX_AGE`, debug tools, signal receivers |

**Rule of thumb:** Use connection pooling and keep signals disabled unless you have a specific reason to enable them.
