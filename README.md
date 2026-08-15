<div align="center">
  <img src="docs/logo.png" alt="Django-Bolt" width="400"/>

  <h3>The fastest Python web framework — built on Django</h3>

  <p>
    Rust-powered HTTP, msgspec serialization, full type validation —<br/>
    with the Django ORM, Django Admin, and every Django package you already use.
  </p>

  <p>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/pypi/v/django-bolt.svg?color=blue" alt="PyPI"/></a>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/pypi/pyversions/django-bolt.svg" alt="Python versions"/></a>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/badge/Django-4.2%20%7C%205.x%20%7C%206.0-0C4B33?logo=django&logoColor=white" alt="Django versions"/></a>
    <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"/></a>
    <br/>
    <a href="https://pepy.tech/projects/django-bolt"><img src="https://static.pepy.tech/personalized-badge/django-bolt?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads" alt="Downloads"/></a>
    <a href="https://discord.gg/4xErptXK82"><img src="https://img.shields.io/discord/1513537500000292894?logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord"/></a>
    <a href="https://deepwiki.com/FarhanAliRaza/django-bolt"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"/></a>
    <a href="https://opencollective.com/django-bolt"><img src="https://img.shields.io/badge/Sponsor-Django%20Bolt-ff69b4?logo=opencollective&logoColor=white" alt="Sponsor"/></a>
  </p>

  <p>
    <a href="https://bolt.farhana.li/"><b>Documentation</b></a> ·
    <a href="#-quick-start"><b>Quick Start</b></a> ·
    <a href="#-features"><b>Features</b></a> ·
    <a href="#-benchmarks"><b>Benchmarks</b></a> ·
    <a href="https://www.youtube.com/watch?v=Pukr-fT4MFY"><b>Video Tutorial</b></a> ·
    <a href="https://discord.gg/4xErptXK82"><b>Discord</b></a>
  </p>
</div>

---

**Django-Bolt** is the fastest Python web framework: **300k+ requests/second** on a single 12-core desktop (8 processes, C=100, loopback), ahead of FastAPI and Robyn, and even of Bun-based JavaScript frameworks (Elysia, Hono) on JSON payloads. It is a fully typed API framework for Django. It serves your endpoints from a Rust HTTP server ([Actix Web](https://actix.rs/) + [Tokio](https://tokio.rs/)), bridges to your Python handlers with [PyO3](https://pyo3.rs/), and serializes with [msgspec](https://jcristharif.com/msgspec/) — while everything you love about Django (ORM, Admin, auth, middleware, signals, third-party apps) keeps working out of the box.

Think Django REST Framework or Django Ninja, with a Rust engine underneath and **no gunicorn or uvicorn required**.

```python
from django_bolt import BoltAPI

api = BoltAPI()

@api.get("/hello/{name}")
async def hello(name: str):
    return {"message": f"Hello, {name}!"}
```

```bash
python manage.py runbolt --dev
```

## ✨ Why Django-Bolt?

| | |
| --- | --- |
| ⚡ **Rust speed, Python ergonomics** | HTTP parsing, routing, auth, guards, CORS, rate limiting, and compression run in Rust without touching the GIL. Your handlers stay plain Python. |
| 🐍 **100% Django** | Use your existing models, `settings.py`, `INSTALLED_APPS`, Django Admin, middleware, and signals. Migrate one endpoint at a time from DRF. |
| 🧷 **Fully typed** | Type hints drive path/query/header/cookie/form/body extraction and validation. `msgspec.Struct` and Bolt `Serializer` return types are validated on the way out. |
| 🚀 **Deploy directly** | `runbolt` *is* the production server: multi-process with `SO_REUSEPORT`, worker recycling, graceful shutdown, static & media serving. |
| 📚 **Batteries included** | OpenAPI docs (Swagger, ReDoc, Scalar, RapiDoc, Stoplight), JWT/API-key auth, guards, pagination, ViewSets, WebSockets, SSE, streaming, testing client, MCP servers. |

## 🚀 Quick Start

### 1. Install

```bash
pip install django-bolt      # or: uv add django-bolt
```

### 2. Add to `INSTALLED_APPS`

```python
# myproject/settings.py
INSTALLED_APPS = [
    ...,
    "django_bolt",
]
```

### 3. Write your first endpoint

Create an `api.py` next to your `settings.py` (or inside any Django app — Bolt autodiscovers them all):

```python
# myproject/api.py
import msgspec
from django.contrib.auth import get_user_model
from django_bolt import BoltAPI

User = get_user_model()
api = BoltAPI()


class UserSchema(msgspec.Struct):
    id: int
    username: str


@api.get("/users/{user_id}")
async def get_user(user_id: int) -> UserSchema:   # response is type-validated
    user = await User.objects.aget(id=user_id)    # Django ORM, no extra setup
    return {"id": user.id, "username": user.username}
```

### 4. Run

```bash
python manage.py runbolt --dev              # auto-reload for development
python manage.py runbolt --processes 4      # production: multi-process, no gunicorn/uvicorn
```

Your API is live at `http://localhost:8000/users/1` and interactive docs at `http://localhost:8000/docs`.

📖 **Next:** the [Quick Start guide](https://bolt.farhana.li/getting-started/quickstart/) → [Deployment](https://bolt.farhana.li/getting-started/deployment/) → [Topic guides](https://bolt.farhana.li/topics/routing/).

## 🧭 A tour of the API

<details open>
<summary><b>Request validation with type hints</b></summary>

```python
import msgspec
from typing import Annotated
from django_bolt import BoltAPI
from django_bolt.param_functions import Header

api = BoltAPI()

class CreateUser(msgspec.Struct):
    username: str
    email: str

@api.post("/users", status_code=201)
async def create_user(
    user: CreateUser,                                     # JSON body → validated struct
    api_key: Annotated[str, Header("x-api-key")],         # header
    page: int = 1,                                        # query param with default
):
    return {"username": user.username, "page": page}
```

</details>

<details>
<summary><b>Authentication & guards (evaluated in Rust)</b></summary>

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated, Requires

IsStaff = Requires("is_staff", True)

@api.get("/admin/stats", auth=[JWTAuthentication()], guards=[IsAuthenticated(), IsStaff])
async def admin_stats(request):
    return {"user_id": request.user.id}
```

JWT signature checks, expiry, API-key lookup, and guard evaluation all happen before the GIL is ever taken.

</details>

<details>
<summary><b>Serializers & ModelViewSet</b></summary>

```python
from django_bolt import ModelViewSet, PageNumberPagination
from django_bolt.serializers import Serializer
from myapp.models import Article

class ArticleSchema(Serializer):
    id: int
    title: str
    content: str

    class Config:
        field_sets = {"list": ["id", "title"]}

@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.all()
    serializer_class = ArticleSchema
    pagination_class = PageNumberPagination
```

One `Serializer` class, many projections — no more `UserListSerializer` / `UserDetailSerializer` / `UserAdminSerializer` sprawl.

</details>

<details>
<summary><b>WebSockets & Server-Sent Events</b></summary>

```python
from django_bolt import WebSocket, StreamingResponse

@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"Echo: {message}")

@api.get("/events")
async def events():
    async def stream():
        for i in range(10):
            yield f"data: tick {i}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
```

</details>

<details>
<summary><b>MCP servers for LLM clients</b></summary>

```python
from bolt_mcp import MCP          # pip install "django-bolt[mcp]"

mcp = MCP("my-server")

@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}

api.mount_mcp(mcp)
```

Expose tools, resources, and prompts over MCP Streamable HTTP, backed by the official Rust SDK.

</details>

<details>
<summary><b>Middleware: CORS, rate limiting, compression</b></summary>

```python
from django_bolt.middleware import cors, rate_limit

@api.get("/public")
@cors(origins=["https://example.com"])
@rate_limit(rps=100, burst=200)
async def public():
    return {"ok": True}
```

Django middleware (sessions, messages, CSRF, your own) is supported too.

</details>

## 📦 Features

| Feature | Description |
| --- | --- |
| ⚡ [High Performance](https://bolt.farhana.li/) | Actix Web + Tokio + PyO3, zero-copy routing, sync-dispatch bypass for simple handlers |
| 🔐 [Authentication](https://bolt.farhana.li/topics/authentication/) | JWT, API key, and Django session auth — validated in Rust |
| 🛡️ [Permissions & Guards](https://bolt.farhana.li/topics/permissions/) | `IsAuthenticated`, `AllowAny`, and claim-based `Requires(...)` guards |
| 🎛️ [Middleware](https://bolt.farhana.li/topics/middleware/) | CORS, rate limiting, [compression](https://bolt.farhana.li/topics/compression/), Django middleware integration |
| 📦 [Serializers](https://bolt.farhana.li/topics/serializers/) | msgspec-based validation with field sets, computed fields, and model integration |
| 🗄️ [Async ORM](https://bolt.farhana.li/topics/async-orm/) | Return QuerySets from async handlers; bounded, vendor-aware ORM executor |
| 📡 [Responses](https://bolt.farhana.li/topics/responses/) | JSON, HTML, redirects, files, streaming, [SSE](https://bolt.farhana.li/topics/sse/) |
| 🔌 [WebSockets](https://bolt.farhana.li/topics/websocket/) | FastAPI-style WebSocket handlers on Rust infrastructure |
| 📚 [OpenAPI](https://bolt.farhana.li/topics/openapi/) | Auto-generated schema with Swagger, ReDoc, Scalar, RapiDoc, and Stoplight UIs |
| 🧱 [Class-Based Views](https://bolt.farhana.li/topics/class-based-views/) | `APIView`, `ViewSet`, `ModelViewSet`, `@action` |
| 📄 [Pagination](https://bolt.farhana.li/topics/pagination/) | PageNumber, LimitOffset, and Cursor pagination |
| 💉 [Dependency Injection](https://bolt.farhana.li/topics/dependencies/) | `Depends(...)` with registration-time graph resolution |
| 🤖 [MCP Servers](https://bolt.farhana.li/topics/mcp/) | Tools, resources, prompts, and streaming over MCP Streamable HTTP |
| 🗂️ [Static & Media Files](https://bolt.farhana.li/topics/static-files/) | Native Rust static/media serving — no WhiteNoise needed |
| 🔗 [ASGI Mounts](https://bolt.farhana.li/topics/asgi-mounts/) | Mount existing ASGI apps under a prefix |
| 🩺 [Health, Logging, Lifespan](https://bolt.farhana.li/topics/health-checks/) | Health endpoints, [structured logging](https://bolt.farhana.li/topics/logging/), [lifespan hooks](https://bolt.farhana.li/topics/lifespan/), [signals](https://bolt.farhana.li/topics/signals/) |
| 🧪 [Testing](https://bolt.farhana.li/topics/testing/) | In-process `TestClient` that runs the full Rust pipeline |
| 🧬 [Nanodjango](https://bolt.farhana.li/topics/nanodjango/) | Single-file Django apps |

All runtime settings and environment variables are listed in the [Settings reference](https://bolt.farhana.li/ref/settings/).

## 📊 Benchmarks

Measured with [bombardier](https://github.com/codesenberg/bombardier) on a single 12-core desktop (Ryzen 5 5600G), loopback, `C=100`, `N=100000`, **8 processes × 1 worker** (`runbolt --processes 8`). Absolute numbers are hardware-specific; run `just save-bench` to reproduce on your machine. Full results: [`bench/BENCHMARK_BASELINE.md`](bench/BENCHMARK_BASELINE.md).

| Endpoint | Requests/sec | p99 latency |
| --- | ---: | ---: |
| Root JSON (`{"message": ...}`) | **~311,000** | 2.2 ms |
| Path + query params (`/items/1?q=hello`) | **~264,000** | — |
| PUT JSON body (`/items/1`) | **~257,000** | — |
| JSON parse + validate (POST) | **~251,000** | — |
| Form data (POST) | **~218,000** | — |
| 10 KB JSON response | **~187,000** | 2.2 ms |
| File upload (multipart) | **~178,000** | — |
| JWT-authenticated (no DB) | **~160,000** | — |
| Static 1 KB asset | **~159,000** | — |
| ORM list, 10 rows (SQLite, async) | **~21,000–27,000** | — |

**Server-Sent Events, 10,000 concurrent clients for 60 s:** 9,489 msg/s, 100% connections succeeded, ~236 MB RSS, 11.9% average CPU.

### Against JavaScript runtimes

The same JSON payloads served by Django-Bolt, [Elysia](https://elysiajs.com) (Bun), and [Hono](https://hono.dev) (Bun & Node), 8 processes each — see [`bench/js`](bench/js/README.md):

| Payload | Django-Bolt | Elysia / Bun | Hono / Bun | Hono / Node |
| --- | ---: | ---: | ---: | ---: |
| 1 KB JSON | 251k | **264k** | 210k | 97k |
| 10 KB JSON | **157k** | 124k | 111k | 79k |

### Why so fast?

- **Actix Web + Tokio** handle HTTP parsing and responses; **matchit** routes with zero-copy path matching.
- **Auth, guards, CORS, rate limiting, compression** run in Rust — no GIL, no Python per-request overhead.
- **msgspec** serialization is 5–10× faster than the standard library; response bodies cross to Rust zero-copy.
- **Sync-dispatch bypass:** handlers that don't actually await are detected at registration and skip the async bridge entirely.
- **Registration-time precomputation:** parameter extraction, dependency graphs, and middleware are compiled once, reused forever.

## 🏗️ How it works

```
HTTP request
   │
   ▼
Actix Web (Rust) ── routing (matchit) ── CORS · rate limit · compression
   │
   ▼
Auth & guards (Rust, no GIL) ── JWT / API key / session · IsAuthenticated · Requires(...)
   │
   ▼
Dispatch ── sync fast path (single GIL block)  or  async path (persistent worker loop)
   │
   ▼
Your handler ── typed params · Depends(...) · Django ORM
   │
   ▼
msgspec serialization ── zero-copy body ── HTTP response
```

## 🚢 Deployment

```bash
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 4
python manage.py runbolt --processes 4 --max-rss 512   # recycle workers above 512 MB
```

Multi-process scaling uses `SO_REUSEPORT` for kernel-level load balancing. Worker recycling, crash respawn, graceful shutdown, and WebSocket drain are built in. See the [Deployment guide](https://bolt.farhana.li/getting-started/deployment/) for systemd, supervisor, and reverse-proxy setups.

## 🤝 Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup (Rust toolchain, `uv`, `just`), the test workflow, and pull request guidelines.

```bash
git clone https://github.com/dj-bolt/django-bolt.git && cd django-bolt
uv sync && just build && just test-py
```

## 💬 Community

- 📖 [Documentation](https://bolt.farhana.li/)
- 💬 [Discord](https://discord.gg/4xErptXK82)
- 🐛 [Issues](https://github.com/dj-bolt/django-bolt/issues)
- 🎥 [Video walkthrough by BugBytes](https://www.youtube.com/watch?v=Pukr-fT4MFY)
- 🤖 [Ask DeepWiki](https://deepwiki.com/FarhanAliRaza/django-bolt) · for AI assistants: [llms.txt](https://bolt.farhana.li/llms.txt)
- ❓ [FAQ](https://bolt.farhana.li/faq/) · [Comparison vs Ninja / DRF / FastAPI / Litestar](https://bolt.farhana.li/comparison/) · [How it works](https://bolt.farhana.li/architecture/)

## 💖 Sponsors

Support Django-Bolt's development by [becoming a sponsor](https://opencollective.com/django-bolt). Your logo will appear here with a link to your website.

<a href="https://opencollective.com/django-bolt/tiers/sponsor/0/website" target="_blank"><img src="https://opencollective.com/django-bolt/tiers/sponsor/0/avatar.svg" /></a>

### Backers

<a href="https://opencollective.com/django-bolt#backers" target="_blank"><img src="https://opencollective.com/django-bolt/backers.svg?width=890" /></a>

## 🙏 Acknowledgments

Django-Bolt stands on the shoulders of giants:

- **[Django REST Framework](https://github.com/encode/django-rest-framework)** — ViewSet patterns, permission system, and overall API philosophy
- **[FastAPI](https://github.com/fastapi/fastapi)** — dependency injection, parameter extraction, and type-hint-driven design
- **[Litestar](https://github.com/litestar-org/litestar)** — OpenAPI plugin architecture, middleware and guard design
- **[Robyn](https://github.com/sparckles/Robyn)** — proved the potential of Rust-powered Python web frameworks with PyO3
- **[Actix Web](https://github.com/actix/actix-web)**, **[PyO3](https://github.com/PyO3/pyo3)**, **[msgspec](https://github.com/jcrist/msgspec)**, **[matchit](https://github.com/ibraheemdev/matchit)** — the foundations that make the speed possible

## 📄 License

Django-Bolt is released under the [MIT License](https://opensource.org/licenses/MIT).
