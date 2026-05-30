---
icon: lucide/upload
---

# Media Files

Django-Bolt can serve user-uploaded media files (`MEDIA_ROOT`) directly from Rust, using the same high-performance handler as [static files](static-files.md). It's enabled automatically when both `MEDIA_URL` and `MEDIA_ROOT` are configured — no extra wiring.

!!! warning "Offload media to a dedicated server in production"
    Django-Bolt serves media *performantly*, but **we don't advise using it as your production media server.** Put a reverse proxy (Nginx), a CDN, or object storage (S3/GCS/R2) in front of `MEDIA_ROOT` and let it serve uploads directly. Built-in media serving is meant for development, internal tools, and small deployments — not as the primary delivery path for user content.

    See [Production: offload media](#production-offload-media) for why and how.

## Configuration

Django-Bolt reads your existing Django media settings:

```python
# settings.py

# URL prefix for media files
MEDIA_URL = "/media/"

# Directory where uploaded files are stored
MEDIA_ROOT = BASE_DIR / "media"
```

When both are set and `MEDIA_ROOT` exists on disk, Django-Bolt mounts a `/media/` scope served entirely in Rust. `GET` and `HEAD` are supported, with ETag/Last-Modified caching, conditional requests (`304 Not Modified`), and range requests — the same capabilities as static serving.

If `MEDIA_ROOT` doesn't exist, the scope isn't registered and a startup warning is printed.

## Security model

User uploads are untrusted input, so media serving applies stricter defaults than static serving.

### Script-bearing uploads are force-downloaded

A file uploaded as `evil.html` or `avatar.svg` can contain JavaScript that runs **in your site's origin** if a browser renders it. `X-Content-Type-Options: nosniff` alone does not stop this — when the `Content-Type` is `text/html` or `image/svg+xml`, browsers *honor* that type and execute any embedded scripts.

So for media, any upload whose extension can carry executable script is rewritten to `Content-Type: application/octet-stream` with `Content-Disposition: attachment`, forcing the browser to download rather than render it:

| Family | Extensions |
|--------|------------|
| HTML | `html` `htm` `xhtml` `xhtm` `shtml` `shtm` `htc` `hta` |
| SVG | `svg` `svgz` |
| XML / XSLT | `xml` `xsl` `xslt` |
| JavaScript | `js` `mjs` `cjs` |
| WebAssembly | `wasm` |

Inert types — images (`png`, `jpg`, `gif`, `webp`), `pdf`, `txt`, `json`, `css` — keep their native content type and render inline as normal.

!!! note "Static files are not disarmed"
    This rewrite applies to **media only**. Static files are admin-curated (a `theme.html` you shipped is trusted), so they keep their native content types.

### Other protections

- **`nosniff` on every response** — including 404s, so a crafted `/media/...` miss can't be MIME-sniffed into executing.
- **Dotfile deny** — any path with a leading-dot component (`.env`, `.git/config`, `.htaccess`, `.ssh/...`) returns `404`, matching the nginx/Apache default. Backslash-separated components are caught too.
- **No directory traversal** — paths containing `..` are rejected with `400`; resolved symlink targets must stay inside `MEDIA_ROOT`.
- **No Django finders fallback** — unlike static, media never falls through to Django's staticfiles finders (they only know `STATICFILES_DIRS`, never `MEDIA_ROOT`), so static assets can't leak under `/media/`.

## Cache-Control

Set `BOLT_MEDIA_MAX_AGE` (seconds) to emit a `Cache-Control` header on successful media responses:

```python
# settings.py
BOLT_MEDIA_MAX_AGE = 3600  # 1 hour
```

This produces `Cache-Control: private, max-age=3600`. Media uses **`private`** (not `public`) on purpose: media is per-user content where the URL is often the only access gate, and `public` would let a shared cache or CDN hand one user's upload to another.

- The header is only set on `2xx` responses — a `404` is never cached with a long `max-age`, so a file becomes visible the moment it's uploaded.
- Missing, non-integer, boolean, or negative values are ignored with a startup warning (no header emitted).

See also [`SECURE_CSP`](static-files.md#content-security-policy-csp) — when configured, the CSP header is applied to media responses as well.

## Production: offload media

For production, serve media from something other than your application server:

- **It's an attack surface kept off your app.** A CDN or object store serves untrusted user content from a separate origin, away from your API and its cookies.
- **It frees app connections.** Streaming large or slow media downloads ties up connections that should be answering API requests.
- **Local `MEDIA_ROOT` isn't shared.** Across multiple processes or hosts, a file uploaded on one machine isn't visible on another. Object storage solves this; a local directory doesn't.
- **You get distribution and lifecycle for free.** CDNs and object stores give geographic edge caching, signed URLs, lifecycle/retention policies, and offsite durability.

### Nginx

Serve `/media/` straight from disk so requests never reach Django-Bolt:

```nginx
location /media/ {
    alias /path/to/your/project/media/;
    expires 1h;
    add_header Cache-Control "private";
    add_header X-Content-Type-Options nosniff;
}
```

!!! warning "Replicate the download-forcing for untrusted uploads"
    Django-Bolt's built-in handler force-downloads script-bearing uploads (see above). If you move media to Nginx or another server, **you lose that protection** unless you reconfigure it there — force `Content-Disposition: attachment` (or `application/octet-stream`) for user-uploaded HTML/SVG/JS, or serve untrusted media from a separate, cookieless domain.

### Object storage (S3 / GCS / R2)

For most production apps, store uploads in object storage via [`django-storages`](https://django-storages.readthedocs.io/) and serve them from the bucket/CDN. Set `Content-Disposition: attachment` on untrusted uploads at upload time, and use signed URLs for access control. With object storage, `MEDIA_URL` points at the bucket/CDN and Django-Bolt isn't in the media request path at all.

## See also

- [Static Files](static-files.md) — serving `STATIC_ROOT` / `STATICFILES_DIRS` and admin assets
- [Settings Reference](../ref/settings.md#static-and-media-file-serving) — `BOLT_MEDIA_MAX_AGE`, `BOLT_STATIC_MAX_AGE`
- [Deployment](../getting-started/deployment.md) — running behind Nginx
