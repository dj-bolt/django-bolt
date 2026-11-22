---
description: Add liveness and readiness routes to a Django-Bolt API, write your own checks, and return 503 for Kubernetes and load balancers.
---

# Health checks

A health check is a route that a load balancer or Kubernetes calls to see if a process can serve.
`django_bolt.health` gives a liveness route, a readiness route and a database check.

```python title="blog/api.py"
from django_bolt import BoltAPI
from django_bolt.health import register_health_checks

api = BoltAPI()
register_health_checks(api)
```

`register_health_checks()` adds two `GET` routes to the API:

- `/health` answers when the process runs. It does no other work.
- `/ready` runs the checks and reports each result. It returns `503 Service Unavailable` when a check fails.

```bash
curl localhost:8000/health
```

```json
{"status": "ok"}
```

```bash
curl localhost:8000/ready
```

```json
{"status": "healthy", "checks": {"check_database": {"healthy": true, "message": "Database connection OK"}}}
```

`/ready` always runs `check_database`.
This check opens a new connection to the `default` database, and closes it. Thus it finds a database that stopped since the last request.

## Add a check

A check is an `async` function with no arguments. It returns a tuple: `True` or `False`, and a message.
Add it with `add_health_check()`:

```python title="blog/api.py"
from django_bolt import BoltAPI
from django_bolt.health import add_health_check, register_health_checks

api = BoltAPI()
register_health_checks(api)


async def check_search_index() -> tuple[bool, str]:
    return False, "Search index is rebuilding"


async def check_cache() -> tuple[bool, str]:
    raise ConnectionError("cache refused the connection")


add_health_check(check_search_index)
add_health_check(check_cache)
```

```bash
curl -i localhost:8000/ready
```

```http
HTTP/1.1 503 Service Unavailable
content-type: application/json

{
  "status": "unhealthy",
  "checks": {
    "check_database": {"healthy": true, "message": "Database connection OK"},
    "check_search_index": {"healthy": false, "message": "Search index is rebuilding"},
    "check_cache": {"healthy": false, "message": "Check failed: cache refused the connection"}
  }
}
```

- The checks run at the same time. The name of each check is the name of its function.
- A check that raises an exception is unhealthy. Its message is `Check failed: ` and the text of the exception.
- `status` is `"unhealthy"` when one check or more is unhealthy. The status code is then `503`.
- `check_database` runs first. `add_health_check()` adds a check after it.
- The list of checks is one list for the process. Add the checks at import time, in `api.py`.

## Write your own health routes

Kubernetes and most load balancers read only the status code. Return `503` when a check fails.
Use `HealthCheck` to run a list of checks of your own:

```python title="blog/api.py"
from django_bolt import JSON, BoltAPI
from django_bolt.auth import AllowAny
from django_bolt.health import HealthCheck, check_database

api = BoltAPI()

readiness = HealthCheck()
readiness.add_check(check_database)


@api.get("/health", guards=[AllowAny()])
async def health() -> dict:
    return {"status": "ok"}


@api.get("/ready", guards=[AllowAny()])
async def ready() -> dict:
    result = await readiness.run_checks()
    status_code = 200 if result["status"] == "healthy" else 503
    return JSON(result, status_code=status_code)
```

When the database answers:

```http
HTTP/1.1 200 OK
content-type: application/json

{"status":"healthy","checks":{"check_database":{"healthy":true,"message":"Database connection OK"}}}
```

When Django cannot open the database, here an SQLite file in a directory that does not exist:

```http
HTTP/1.1 503 Service Unavailable
content-type: application/json

{"status":"unhealthy","checks":{"check_database":{"healthy":false,"message":"Database error: unable to open database file"}}}
```

`HealthCheck` has two methods: `add_check(check)` and `await run_checks()`.
`run_checks()` returns the dict that `/ready` sends.

## Keep health routes public

A health route gets the default guards, as each other route does.
With `BOLT_DEFAULT_PERMISSION_CLASSES = [IsAuthenticated()]`, the routes of `register_health_checks()` need a user:

```bash
curl -i localhost:8000/health
```

```http
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"Authentication required"}
```

A probe sends no credentials. Write the routes yourself with `guards=[AllowAny()]`, as in [Write your own health routes](#write-your-own-health-routes).
See [Permissions](permissions.md).

## Configure Kubernetes probes

Use `/health` for the liveness probe and `/ready` for the readiness probe:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  periodSeconds: 5
  failureThreshold: 3
```

Do not run database checks in the liveness probe. A database outage then restarts each pod, and a restart does not fix the database.
Each call of `check_database` opens a database connection. Choose a `periodSeconds` that your database can serve for each pod.

Set `terminationGracePeriodSeconds` above the `--workers-kill-timeout` of `runbolt`. See [Deployment](deployment.md#stop-the-server-gracefully).

## Health checks and the access log

An API with no `logging_config` writes no access-log line for `/health`, `/ready` and `/metrics`.
The match is on the complete path. With `BoltAPI(prefix="/api")`, the path is `/api/health`, and its lines appear.
Add the path to `skip_paths` to remove them. See [Logging](logging.md#skip-paths-and-status-codes).

## See also

- [Deployment](deployment.md)
- [Logging](logging.md)
- [Permissions](permissions.md)
- [Responses](responses.md)
