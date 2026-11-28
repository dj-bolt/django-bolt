---
description: Limit how often a client can call a route. Count per IP address, per user, per API key or per header, and answer 429.
---

# Rate limiting

A rate limit caps the number of requests that one client can send to a route.
Use it on routes that are expensive or easy to abuse, such as login, search or comment creation.
Django-Bolt counts requests in Rust, before any Python code. A limit per IP address or per header runs before authentication. A limit per user or per API key runs after it.

## Limit a route

Put `@rate_limit` below the route decorator:

```python title="blog/api.py"
from django_bolt import BoltAPI, rate_limit

api = BoltAPI()


@api.post("/comments")
@rate_limit(rps=2, burst=3)
async def create_comment() -> dict:
    return {"created": True}
```

A client can send 3 requests at once. After that, it gets 2 more requests each second.
Five quick requests from one address give 200, 200, 200, 429, 429:

```bash
curl -i -X POST localhost:8000/comments
```

```http
HTTP/1.1 429 Too Many Requests
retry-after: 1
x-ratelimit-limit: 2
x-ratelimit-burst: 3
content-type: application/json

{"detail":"Rate limit exceeded. Try again in 1 seconds.","retry_after":1}
```

`retry-after` is the number of seconds to wait, at least 1. The handler does not run for a 429.
Bolt prints one line to stderr for each bucket and each second of rejections:

```text
[django-bolt] Rate limit exceeded: POST /comments | key: 127.0.0.1 | limit: 2 rps (burst: 3) | retry after: 1s | suppressed: 0
```

The line names an address. It shows a hash in place of a header value or a user ID, so it never logs a credential.

## Choose `rps` and `burst`

Bolt uses a token bucket for each client and route:

- The bucket holds up to `burst` tokens, and it starts full.
- Each request takes one token. A request that finds no token gets 429.
- The bucket gets `rps` new tokens each second.

`rps` is the long-run rate. `burst` is the size of a short peak. The default is `rps=100` and `burst` two times `rps`.
Both must be an `int` of 1 or more. Another value raises `TypeError` or `ValueError` when you decorate the handler.

## Count per user, per API key or per header

`key` selects what Bolt counts:

| `key` | One bucket for each | Runs |
| --- | --- | --- |
| `"ip"` (default) | Client address. An IPv6 client counts per /64 network. | Before authentication |
| `"user"` | Authenticated identity of any backend | After authentication |
| `"api_key"` | Authenticated API key | After authentication |
| A header name, such as `"x-tenant-id"` | Value of that header | Before authentication |

```python
from django_bolt.auth import JWTAuthentication


@api.get("/feed", auth=[JWTAuthentication()])
@rate_limit(rps=1, burst=2, key="user")
async def feed() -> dict:
    return {"articles": []}


@api.get("/search")
@rate_limit(rps=1, burst=1, key="x-tenant-id")
async def search() -> dict:
    return {"results": []}
```

With `key="user"`, each user has a bucket. The third quick request of `ada` gets 429, and the first request of `bob` gets 200.

A request with no identity counts per client address, for `"user"` and for `"api_key"`.
A Django session has no identity that Rust can read, so a session request counts per address.
`"user"` and `"api_key"` need an auth backend on the route or in `BOLT_AUTHENTICATION_CLASSES`. Without one, startup fails with `ValueError`.

With a header key, the requests that do not send the header share one bucket.
A client chooses the value of a header, so it can get a new bucket for each value. Use a header key only for a header that your proxy sets or checks.

## Set the client address behind a proxy

With `key="ip"`, Bolt counts the address of the TCP peer. Behind a proxy or a load balancer, that is the address of the proxy.
Then all clients share one bucket.

List your proxies in `settings.py`:

```python title="settings.py"
BOLT_TRUSTED_PROXIES = ["10.0.0.0/8"]
```

For a request from a listed proxy, Bolt reads `X-Forwarded-For` from the right. It takes the first address that is not a listed proxy.
A client cannot choose its bucket when the proxy appends the real address after the values that the client sent.
Configure the proxy to do this, for example `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` in nginx. A proxy that passes the header of the client with no change lets the client choose its bucket.
Bolt ignores `X-Forwarded-For` from a peer that is not in the list.

`BOLT_RATE_LIMIT_IPV6_PREFIX` sets the network size of an IPv6 client. The default is 64. Set 128 to count each IPv6 address.
See the [settings reference](../ref/settings.md).

## Limit a group of routes

A `BoltAPI` or a `Router` takes a rate limit dict in `middleware`. Each of its routes gets its own bucket with that limit.
`@skip_middleware("rate_limit")` removes the limit from one route:

```python
from django_bolt import Router, skip_middleware

public = Router(prefix="/public", middleware=[{"type": "rate_limit", "rps": 20, "burst": 40}])


@public.get("/articles")
async def public_articles() -> list:
    return []


@public.get("/health")
@skip_middleware("rate_limit")
async def health() -> dict:
    return {"status": "ok"}


api.include_router(public)
```

The most specific limit wins: the route, then the router, then the API. The outer limit does not also apply.
A route takes one limit. A second `@rate_limit` on the same function raises `ValueError`.

## Know the limits

- Each `runbolt` process has its own buckets. With `--processes 4`, a client can get up to four times the limit, because the kernel spreads its connections over the processes.
  Divide the limit by the number of processes, or limit at the proxy.
- A restart empties the buckets.
- Each process keeps up to 100,000 buckets. Above that number, Bolt deletes each bucket that is full again.
  Such a bucket holds no state, because a new bucket for its key is the same. When these are fewer than 20% of the buckets, Bolt also deletes other buckets, in no fixed order, up to 20%.
- A 429 has the CORS headers of the route, so a browser page can read it.
- `TestClient` counts requests as `runbolt` does. Each `TestClient` has its own buckets.

## See also

- [Middleware reference](../ref/middleware.md#rate_limit): the arguments of `@rate_limit`.
- [Settings reference](../ref/settings.md): `BOLT_TRUSTED_PROXIES` and `BOLT_RATE_LIMIT_IPV6_PREFIX`.
- [Authentication](authentication.md): the backends that give `key="user"` an identity.
- [Deployment](deployment.md): run `runbolt` behind a proxy.
