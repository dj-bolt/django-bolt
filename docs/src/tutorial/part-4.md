---
description: Tutorial part 4. Test the API with TestClient and the Django test runner, then run runbolt with several processes.
---

# Tutorial part 4: tests and running in production

In this part, you write tests for the articles API, and you start the server as in production.
This part starts where [part 3](part-3.md) stops.

## Write the tests

Replace the contents of `articles/tests.py`:

```python title="articles/tests.py"
from django.contrib.auth.models import User
from django.test import TestCase

from django_bolt.auth import create_jwt_for_user
from django_bolt.testing import TestClient

from .api import api
from .models import Article


class ArticleAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice", password="correct-horse")
        cls.bob = User.objects.create_user("bob", password="battery-staple")
        cls.article = Article.objects.create(
            title="Hello, Bolt", body="My first article.", author=cls.alice, published=True
        )

    def setUp(self):
        self.api_client = self.enterContext(TestClient(api))

    def token_for(self, user):
        return {"Authorization": f"Bearer {create_jwt_for_user(user)}"}

    def test_list_filters_by_published(self):
        Article.objects.create(title="Draft notes", body="Not ready yet.", author=self.alice)
        response = self.api_client.get("/articles?published=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([article["title"] for article in response.json()], ["Hello, Bolt"])

    def test_missing_article_returns_404(self):
        response = self.api_client.get("/articles/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Article 999 not found"})

    def test_create_requires_a_token(self):
        response = self.api_client.post("/articles", json={"title": "Hi", "body": "Text"})
        self.assertEqual(response.status_code, 401)

    def test_create_rejects_an_empty_title(self):
        response = self.api_client.post(
            "/articles", json={"title": "", "body": "Text"}, headers=self.token_for(self.bob)
        )
        self.assertEqual(response.status_code, 422)

    def test_create_sets_the_author(self):
        response = self.api_client.post(
            "/articles", json={"title": "New", "body": "Text"}, headers=self.token_for(self.bob)
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["author_id"], self.bob.id)

    def test_only_the_author_can_edit(self):
        url = f"/articles/{self.article.id}"
        changes = {"title": "Changed", "body": "Text"}
        response = self.api_client.put(url, json=changes, headers=self.token_for(self.bob))
        self.assertEqual(response.status_code, 403)
        response = self.api_client.put(url, json=changes, headers=self.token_for(self.alice))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Changed")
```

`TestClient(api)` sends requests to your API in the test process. It does not open a network port.
Each request goes through the same request pipeline as in `runbolt`. Routing, authentication and guards run in Rust, as on the server.
`self.enterContext()` opens the client for each test and closes it at the end of the test.

The tests use Django's `TestCase`. Each test runs in a transaction, and Django rolls it back at the end.
The handlers use the connection of the test, so they see the rows of `setUpTestData`.

`token_for()` makes a token with `create_jwt_for_user()`. The tests do not call `/token`.

You cannot use `self.client`, the test client of Django, for these routes.
It sends requests to the URL configuration of Django, and the Bolt routes are not in it.

## Run the tests

```bash
python manage.py test articles
```

```text
Found 6 test(s).
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
.
----------------------------------------------------------------------
Ran 6 tests in 0.558s

OK
Destroying test database for alias 'default'...
```

All six tests pass.
Django runs tests with `DEBUG = False`, and the project has no collected static files. Thus each client prints a warning about `/static`. The warning does not change the results of the tests.

To see a failure, change `raise Forbidden(...)` in `get_own_article` to `pass`, and run the tests again.
The output ends with this report (the warnings are not shown here):

```text
======================================================================
FAIL: test_only_the_author_can_edit (articles.tests.ArticleAPITests.test_only_the_author_can_edit)
----------------------------------------------------------------------
Traceback (most recent call last):
  File ".../newsroom/articles/tests.py", line 58, in test_only_the_author_can_edit
    self.assertEqual(response.status_code, 403)
AssertionError: 200 != 403

----------------------------------------------------------------------
Ran 6 tests in 0.644s

FAILED (failures=1)
Destroying test database for alias 'default'...
```

Put the `raise` back before you continue.

To use pytest, see [Testing](../topics/testing.md).

## Run the server with several processes

Stop the development server with `CTRL+C`. Start the server without `--dev`, with four processes:

```bash
python manage.py runbolt --processes 4
```

```text
System check identified no issues (0 silenced).

  ⚡ Django Bolt v0.13.0  ready in 45 ms

  ➜  Local: http://0.0.0.0:8000
  ➜  Docs:  http://0.0.0.0:8000/docs
  ➜  Admin: http://0.0.0.0:8000/admin/

  Mode           production
  Routes         7 API · 1 ASGI · +8 framework · 2 apps
  Workers        4 processes
  Compression    enabled
  Middleware     15 handlers

  press CTRL+C to stop
```

`runbolt` starts four worker processes. Each process has its own Python interpreter, so the processes do not share a GIL.
All the processes listen on port 8000. The operating system gives each new connection to one of them.
The parent process watches the workers. `CTRL+C` stops them all:

```text
  02:12:43 [bolt] Shutting down 4 workers...
```

Without `--dev`, the server does not reload when a file changes.
`--processes` with a value above 1 needs `os.fork()`, so it does not work on Windows.

The API serves the same data:

```bash
curl localhost:8000/articles
```

```json
[{"id":3,"title":"Signed article","body":"Edited by Alice.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:40.524136Z"},{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}]
```

## Before you deploy

The project still has the development settings of `startproject`. Before you put it on a server, do these steps:

1. Set `DEBUG = False` and set `ALLOWED_HOSTS`.
2. Read `SECRET_KEY` from the environment. Bolt signs the tokens with it, so a new key makes all the tokens not valid.
3. Set `STATIC_ROOT` and run `python manage.py collectstatic`. `runbolt` serves the files from `STATIC_ROOT`.
4. Start `runbolt` from a process manager, for example systemd, and put a reverse proxy in front of it for TLS.

The [Deployment](../topics/deployment.md) guide gives the details of each step.

## What you built

You built an API for a publishing site:

- A Django project with an `articles` app, an `Article` model and migrations.
- Five routes that list, read, create, change and delete articles, with validation and query filters.
- A route that issues JWT tokens. Only the author of an article can change it.
- Tests that use `TestClient` and the Django test runner.
- A server with four processes, with no gunicorn or uvicorn.

## Next steps

- [Serializers](../topics/serializers.md): Bolt `Serializer` classes, field validators and model fields.
- [Class-based views](../topics/class-based-views.md): a `ModelViewSet` in place of five handlers.
- [Pagination](../topics/pagination.md): pages for long lists.
- [Middleware](../topics/middleware.md): CORS, rate limiting and Django middleware.
- [Deployment](../topics/deployment.md): process managers, proxies and worker recycling.
