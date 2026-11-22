---
description: Save user uploads to MEDIA_ROOT, serve them from Django-Bolt with safe headers, and serve private files through a handler.
---

# Media files

Django-Bolt serves the files of `MEDIA_ROOT` at `MEDIA_URL`, from Rust, with no call to Python.
Use it for files that users upload, such as images and attachments.

```python title="blog/settings.py"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
```

This handler saves an upload with Django's storage and returns its URL:

```python title="blog/api.py"
from typing import Annotated

from django.core.files.storage import default_storage

from django_bolt import BoltAPI, UploadFile
from django_bolt.params import File

api = BoltAPI()


@api.post("/attachments", status_code=201)
def upload_attachment(file: Annotated[UploadFile, File(max_size=5 * 1024 * 1024)]) -> dict:
    name = default_storage.save(f"attachments/{file.filename}", file.file)
    return {"url": default_storage.url(name)}
```

```bash
curl -F "file=@cover.png" localhost:8000/attachments
```

```json
{"url": "/media/attachments/cover.png"}
```

```bash
curl -I localhost:8000/media/attachments/cover.png
```

```http
HTTP/1.1 200 OK
content-type: image/png
content-disposition: inline; filename="cover.png"
x-content-type-options: nosniff
accept-ranges: bytes
etag: "d2093:9:6b02502b:4f9495d"
last-modified: Sun, 22 Nov 2026 02:15:07 GMT
```

The handler is sync because `default_storage.save()` writes to the disk. A second file with the same name gets a new name, such as `cover_IIDMyr6.png`.
For uploads, see [Requests](requests.md).

## When Bolt serves media

Bolt serves `MEDIA_URL` only when each of these is true:

- `MEDIA_URL` starts with `/`, is not `/`, and has no `{` or `}`.
- `MEDIA_ROOT` is an absolute path.
- `MEDIA_ROOT` is a directory that exists when the server starts.

Otherwise, Bolt prints a warning at startup and does not serve media. For example:

```text
[django-bolt] Warning: MEDIA_URL must start with '/' for in-process serving (got "media/"); ignoring.
[django-bolt] Warning: MEDIA_ROOT must be an absolute path (got "media"); ignoring.
```

Django's defaults, `MEDIA_URL = ""` and `MEDIA_ROOT = ""`, serve no media.
Bolt serves `GET` and `HEAD`. Media responses do not appear in the access log.

## Files that can run script are downloads

A user can upload an `.html` or `.svg` file with JavaScript in it. If a browser shows that file on your domain, the script runs with the cookies of your site.
Thus Bolt sends each media file with one of these extensions as a download:

| Kind | Extensions |
| --- | --- |
| HTML | `html`, `htm`, `xhtml`, `xhtm`, `shtml`, `shtm`, `htc`, `hta` |
| SVG | `svg`, `svgz` |
| XML and XSLT | `xml`, `xsl`, `xslt` |
| JavaScript | `js`, `mjs`, `cjs` |
| WebAssembly | `wasm` |

```bash
curl -I localhost:8000/media/attachments/page.html
```

```http
HTTP/1.1 200 OK
content-type: application/octet-stream
content-disposition: attachment
x-content-type-options: nosniff
```

Other files, such as `.png`, `.pdf`, `.txt` and `.css`, keep their own content type and show in the browser.

Bolt also applies the rules of static files to media:

- A path with `..` gets `400`.
- A path with a part that starts with a dot, such as `.env`, gets `404`.
- A symbolic link that points out of `MEDIA_ROOT` gives `404`.
- Each response, also a `404`, has `X-Content-Type-Options: nosniff`.
- `SECURE_CSP` adds a `Content-Security-Policy` header. See [Static files](static-files.md#add-a-content-security-policy-header).

Bolt never looks for a media file with the staticfiles finders.

## Cache media files

Each response has an `ETag` and a `Last-Modified` header, so a browser can ask with `If-None-Match` and get `304`.
Set `BOLT_MEDIA_MAX_AGE` in seconds to add a `Cache-Control` header:

```python title="blog/settings.py"
BOLT_MEDIA_MAX_AGE = 3600  # 1 hour
```

```http
cache-control: private, max-age=3600
```

The header is `private`, so a shared cache or a CDN does not give one user's file to another user.
Bolt adds it only to a `2xx` response. A value that is not an `int` of 0 or more stops startup with `ImproperlyConfigured`.

## Serve private files

Each file in `MEDIA_ROOT` is public. A client that knows the URL gets the file, with no authentication.
Keep private files out of `MEDIA_ROOT`, and serve them from a handler with a guard:

```python title="blog/api.py"
from django.conf import settings

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated
from django_bolt.exceptions import NotFound
from django_bolt.responses import FileResponse

api = BoltAPI()

PRIVATE_ROOT = settings.BASE_DIR / "private"


@api.get("/invoices/{invoice_id}/pdf", guards=[IsAuthenticated()])
async def invoice_pdf(invoice_id: int) -> FileResponse:
    path = PRIVATE_ROOT / "invoices" / f"{invoice_id}.pdf"
    if not path.is_file():
        raise NotFound(detail="Invoice not found")
    return FileResponse(str(path), filename=f"invoice-{invoice_id}.pdf")
```

With no token, the response is `401` with `{"detail":"Authentication required"}`.
With a valid token, the response is the file:

```http
HTTP/1.1 200 OK
content-type: application/pdf
content-disposition: attachment; filename="invoice-7.pdf"
```

Set `BOLT_ALLOWED_FILE_PATHS = [BASE_DIR / "private"]` to stop a `FileResponse` outside that directory. See [Responses](responses.md).

## Serve media in production

Bolt can serve media in production. A reverse proxy, a CDN or an object store is often a better choice:

- Two servers do not share a local `MEDIA_ROOT`. A file uploaded on one host is missing on the other. Object storage, for example S3 through [django-storages](https://django-storages.readthedocs.io/), solves this.
- A large download keeps a connection of your API server open.
- A separate domain for user files keeps the files away from the cookies of your site.

When nginx serves `MEDIA_ROOT`, the download rule of Bolt does not apply. Add it yourself:

```nginx
location /media/ {
    alias /srv/blog/media/;
    add_header X-Content-Type-Options nosniff;
}

location ~* ^/media/(.+\.(?:html?|xhtml?|shtml?|htc|hta|svgz?|xml|xslt?|m?js|cjs|wasm))$ {
    alias /srv/blog/media/$1;
    types { }
    default_type application/octet-stream;
    add_header Content-Disposition attachment;
    add_header X-Content-Type-Options nosniff;
}
```

## See also

- [Static files](static-files.md)
- [Requests](requests.md)
- [Responses](responses.md)
- [Deployment](deployment.md)
- [Settings reference](../ref/settings.md#static-and-media-files)
