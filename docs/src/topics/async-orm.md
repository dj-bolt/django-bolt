---
icon: lucide/database
---

# Async Django ORM

Django-Bolt handlers are async, which means you use Django's async ORM methods. This guide explains how to work with the ORM efficiently and avoid common pitfalls.

## Why async handlers?

Django-Bolt uses async handlers for maximum performance. The Rust runtime manages concurrent requests while your Python code handles business logic. Django 5.0+ provides excellent async ORM support.

## Basic async ORM methods

Use the `a`-prefixed versions of ORM methods:

```python
from myapp.models import Article

# Get a single object
article = await Article.objects.aget(id=1)

# Create an object
article = await Article.objects.acreate(
    title="My Article",
    content="Content here"
)

# Get or create
article, created = await Article.objects.aget_or_create(
    title="My Article",
    defaults={"content": "Default content"}
)

# Count
total = await Article.objects.acount()

# Check existence
exists = await Article.objects.filter(published=True).aexists()

# Delete
deleted_count, _ = await Article.objects.filter(draft=True).adelete()

# Update
updated_count = await Article.objects.filter(draft=True).aupdate(published=True)
```

## Iterating over querysets

Use `async for` to iterate over querysets:

```python
@api.get("/articles")
async def list_articles():
    articles = []
    async for article in Article.objects.filter(published=True)[:20]:
        articles.append({
            "id": article.id,
            "title": article.title
        })
    return {"articles": articles}
```

## QuerySet evaluation and the N+1 problem

**This is the most important section.** Understanding when querysets are evaluated prevents performance issues.

### The problem

When you return a queryset or access related objects, Django evaluates the query. If related objects aren't prefetched, each access triggers a new database query:

```python
# BAD: N+1 queries
@api.get("/articles")
async def list_articles():
    articles = []
    async for article in Article.objects.all()[:20]:
        # Each article.author triggers a separate query!
        articles.append({
            "id": article.id,
            "title": article.title,
            "author_name": article.author.username  # N+1 query here!
        })
    return {"articles": articles}
```

With 20 articles, this executes 21 queries: 1 for articles + 20 for authors.

### The solution: select_related and prefetch_related

Use `select_related` for ForeignKey and OneToOne relationships:

```python
# GOOD: 1 query with JOIN
@api.get("/articles")
async def list_articles():
    articles = []
    async for article in Article.objects.select_related("author")[:20]:
        articles.append({
            "id": article.id,
            "title": article.title,
            "author_name": article.author.username  # No extra query!
        })
    return {"articles": articles}
```

Use `prefetch_related` for ManyToMany and reverse ForeignKey relationships:

```python
# GOOD: 2 queries (articles + tags)
@api.get("/articles")
async def list_articles():
    articles = []
    queryset = Article.objects.select_related("author").prefetch_related("tags")
    async for article in queryset[:20]:
        articles.append({
            "id": article.id,
            "title": article.title,
            "author_name": article.author.username,
            "tags": [tag.name for tag in article.tags.all()]  # Already prefetched!
        })
    return {"articles": articles}
```

### When to use which

| Relationship | Method | Queries |
|--------------|--------|---------|
| ForeignKey | `select_related` | 1 (SQL JOIN) |
| OneToOneField | `select_related` | 1 (SQL JOIN) |
| ManyToManyField | `prefetch_related` | 2 (separate query) |
| Reverse ForeignKey | `prefetch_related` | 2 (separate query) |

### Chaining optimizations

Combine both for complex queries:

```python
@api.get("/posts/{post_id}")
async def get_post(post_id: int):
    post = await (
        BlogPost.objects
        .select_related("author")                    # ForeignKey
        .prefetch_related("tags", "comments__author") # M2M and nested FK
        .aget(id=post_id)
    )

    return {
        "id": post.id,
        "title": post.title,
        "author": {"id": post.author.id, "name": post.author.username},
        "tags": [{"id": t.id, "name": t.name} for t in post.tags.all()],
        "comments": [
            {
                "id": c.id,
                "text": c.text,
                "author": c.author.username  # Prefetched via comments__author
            }
            for c in post.comments.all()
        ]
    }
```

## Returning querysets directly

Django-Bolt can serialize querysets directly, but be careful:

```python
# This works, but may cause N+1 if serializer accesses relations
@api.get("/articles")
async def list_articles():
    return Article.objects.all()[:20]
```

**Always optimize before returning:**

```python
# Better: optimize before returning
@api.get("/articles")
async def list_articles():
    return Article.objects.select_related("author").prefetch_related("tags")[:20]
```

## Working with serializers

When using `msgspec.Struct` serializers, the same rules apply:

```python
import msgspec

class AuthorSchema(msgspec.Struct):
    id: int
    username: str

class ArticleSchema(msgspec.Struct):
    id: int
    title: str
    author: AuthorSchema  # Nested!

@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleSchema:
    # MUST use select_related for the nested author
    article = await Article.objects.select_related("author").aget(id=article_id)

    return ArticleSchema(
        id=article.id,
        title=article.title,
        author=AuthorSchema(
            id=article.author.id,
            username=article.author.username
        )
    )
```

## Pagination with optimizations

Always combine pagination with query optimization:

```python
@api.get("/articles")
async def list_articles(page: int = 1, page_size: int = 20):
    offset = (page - 1) * page_size

    # Optimize the base queryset
    queryset = (
        Article.objects
        .select_related("author")
        .prefetch_related("tags")
        .filter(published=True)
        .order_by("-created_at")
    )

    # Get count and page
    total = await queryset.acount()
    articles = []
    async for article in queryset[offset:offset + page_size]:
        articles.append({
            "id": article.id,
            "title": article.title,
            "author": article.author.username,
            "tags": [t.name for t in article.tags.all()]
        })

    return {
        "items": articles,
        "total": total,
        "page": page,
        "pages": (total + page_size - 1) // page_size
    }
```

## ViewSet optimization

In ViewSets, optimize the queryset at the class level:

```python
from django_bolt.views import ViewSet

@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    # Optimize here - applies to all actions
    queryset = Article.objects.select_related("author").prefetch_related("tags")

    async def list(self, request) -> list[dict]:
        articles = []
        async for article in await self.get_queryset():
            articles.append(self.serialize(article))
        return articles

    async def retrieve(self, request) -> dict:
        article = await self.get_object()
        return self.serialize(article)

    def serialize(self, article):
        return {
            "id": article.id,
            "title": article.title,
            "author": article.author.username,
            "tags": [t.name for t in article.tags.all()]
        }
```

## Aggregations

Use async aggregation methods:

```python
from django.db.models import Count, Avg, Q

@api.get("/stats")
async def article_stats():
    stats = await Article.objects.aaggregate(
        total=Count("id"),
        published=Count("id", filter=Q(published=True)),
        avg_comments=Avg("comment_count")
    )
    return stats
```

## Bulk operations

Use async bulk methods for efficiency:

```python
# Bulk create
articles = [
    Article(title=f"Article {i}", content="...")
    for i in range(100)
]
created = await Article.objects.abulk_create(articles)

# Bulk update
await Article.objects.filter(draft=True).aupdate(published=True)
```

## Transactions

Django's `transaction.atomic()` requires `sync_to_async`:

```python
from asgiref.sync import sync_to_async
from django.db import transaction

@api.post("/transfer")
async def transfer_funds(from_id: int, to_id: int, amount: float):
    @sync_to_async
    def do_transfer():
        with transaction.atomic():
            from_account = Account.objects.select_for_update().get(id=from_id)
            to_account = Account.objects.select_for_update().get(id=to_id)

            from_account.balance -= amount
            to_account.balance += amount

            from_account.save()
            to_account.save()

        return {"success": True}

    return await do_transfer()
```

## Common mistakes

### 1. Forgetting async methods

```python
# WRONG: Sync method in async handler
article = Article.objects.get(id=1)  # Raises SynchronousOnlyOperation

# RIGHT: Use async method
article = await Article.objects.aget(id=1)
```

### 2. Sync iteration

```python
# WRONG: Sync for loop
for article in Article.objects.all():  # Raises SynchronousOnlyOperation
    pass

# RIGHT: Async iteration
async for article in Article.objects.all():
    pass
```

### 3. Missing select_related in loops

```python
# WRONG: N+1 queries
async for article in Article.objects.all():
    print(article.author.name)  # Extra query per article!

# RIGHT: Prefetch with select_related
async for article in Article.objects.select_related("author"):
    print(article.author.name)  # No extra query
```

### 4. Evaluating queryset too late

```python
# PROBLEMATIC: QuerySet returned without optimization
@api.get("/articles")
async def list_articles():
    return Article.objects.all()  # Relations not optimized!

# BETTER: Optimize before returning
@api.get("/articles")
async def list_articles():
    return Article.objects.select_related("author")[:20]
```

## Quick reference

| Sync method | Async method |
|-------------|--------------|
| `.get()` | `.aget()` |
| `.create()` | `.acreate()` |
| `.update()` | `.aupdate()` |
| `.delete()` | `.adelete()` |
| `.count()` | `.acount()` |
| `.exists()` | `.aexists()` |
| `.first()` | `.afirst()` |
| `.last()` | `.alast()` |
| `.aggregate()` | `.aaggregate()` |
| `.bulk_create()` | `.abulk_create()` |
| `for x in qs:` | `async for x in qs:` |
| `list(qs)` | `[x async for x in qs]` |

## Performance checklist

Before deploying, verify:

- [ ] All ORM calls use `a`-prefixed methods
- [ ] All loops use `async for`
- [ ] ForeignKey access uses `select_related`
- [ ] ManyToMany access uses `prefetch_related`
- [ ] Nested serializers have corresponding prefetch
- [ ] ViewSet querysets are optimized at class level
- [ ] Pagination includes query optimization
