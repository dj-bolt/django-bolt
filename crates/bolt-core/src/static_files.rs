//! Static file serving with Django integration.
//!
//! Uses actix-files for efficient file serving with proper HTTP semantics:
//! - Streaming (memory efficient for large files)
//! - ETag and Last-Modified headers
//! - If-None-Match / If-Modified-Since support (304 responses)
//! - Range requests for resumable downloads
//! - Content-Type detection
//!
//! File lookup order:
//! 1. Configured directories (STATIC_ROOT, STATICFILES_DIRS) - fast path
//! 2. Django's staticfiles finders (for app static files like admin)

use actix_files::{NamedFile, file_extension_to_mime};
use actix_web::http::header::{self, ContentDisposition, DispositionType};
use actix_web::mime::{self, Mime};
use actix_web::{HttpRequest, HttpResponse, web};
use pyo3::prelude::*;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use crate::state::{FinderRoot, ScopeConfig, ServeDir, ServeMode};

/// True when a browser can run script from a response of this content type.
///
/// The types are HTML, each XML MIME type and each JavaScript MIME type of
/// WHATWG MIME Sniffing (section 4.6), and WebAssembly. A browser runs the
/// XHTML script elements of any XML document, such as SVG, RSS or MathML.
/// `text/x-component` (`.htc`) and `application/hta` (`.hta`) are HTML that
/// Internet Explorer runs. CSS is not in the list: it cannot run script.
/// `Mime` keeps the type and the subtype in lower case.
fn can_run_script(content_type: &Mime) -> bool {
    content_type.suffix() == Some(mime::XML)
        || matches!(
            content_type.essence_str(),
            "text/html"
                | "text/xml"
                | "application/xml"
                | "application/wasm"
                | "text/x-component"
                | "application/hta"
                | "application/ecmascript"
                | "application/javascript"
                | "application/x-ecmascript"
                | "application/x-javascript"
                | "text/ecmascript"
                | "text/javascript"
                | "text/javascript1.0"
                | "text/javascript1.1"
                | "text/javascript1.2"
                | "text/javascript1.3"
                | "text/javascript1.4"
                | "text/javascript1.5"
                | "text/jscript"
                | "text/livescript"
                | "text/x-ecmascript"
                | "text/x-javascript"
        )
}

/// Rejects any path with a leading-dot component. Matches the nginx/Apache
/// default deny for dotfiles, so stray `.env`, `.git/config`, `.htaccess`,
/// `.ssh/...` left in STATIC_ROOT or MEDIA_ROOT aren't reachable over HTTP.
///
/// Operates on the URL-decoded path string. Splits on both `/` and `\` so a
/// Windows-style backslash component (`foo\.env`) is caught as well.
fn has_dotfile_component(relative_path: &str) -> bool {
    relative_path
        .split(['/', '\\'])
        .any(|segment| segment.starts_with('.'))
}

/// Find a static file in the configured directories (fast path).
///
/// `directories` must hold pre-canonicalized absolute paths (server-startup
/// canonicalization is in `start_server`). This keeps `canonicalize()` — a
/// multi-syscall `realpath(3)` — off the per-request directory lookup; it
/// runs only on the resolved file path, where it must (to follow symlinks
/// in the user-supplied portion).
///
/// A directory with a prefix serves only a path that starts with the prefix,
/// and Bolt removes the prefix before the join, as Django's
/// `FileSystemFinder` does. The rest of the path must be a plain relative
/// path too: `vendor//etc/passwd` leaves `/etc/passwd` after the prefix.
///
/// Gives the canonical path to open, and the requested path before symlinks
/// resolve. The requested name gives the content type, as for a finder.
fn find_in_directories(
    relative_path: &str,
    directories: &[ServeDir],
) -> Option<(PathBuf, PathBuf)> {
    // Security: prevent directory traversal. A path of plain names only: no
    // `..`, no root (`/etc`) and, on Windows, no drive or UNC prefix (`C:\`).
    if relative_path.contains("..") || !is_plain_relative(relative_path) {
        return None;
    }

    for dir in directories {
        let rest = match dir.prefix {
            Some(ref prefix) => match relative_path.strip_prefix(prefix.as_str()) {
                Some(rest) if is_plain_relative(rest) => rest,
                _ => continue,
            },
            None => relative_path,
        };
        let full_path = dir.root.join(rest);

        // Canonicalize the *file* (not the dir) to resolve any symlinks in
        // the relative portion, then verify the result is still inside the
        // canonical root. Stops a symlink-out-of-root from escaping.
        if let Ok(canonical) = full_path.canonicalize()
            && canonical.starts_with(&dir.root)
            && canonical.is_file()
        {
            return Some((canonical, full_path));
        }
    }
    None
}

/// True when every part of `path` is a plain name or `.`. A root, a parent
/// and a Windows prefix are not, so `join` cannot leave the static directory.
fn is_plain_relative(path: &str) -> bool {
    Path::new(path)
        .components()
        .all(|part| matches!(part, Component::Normal(_) | Component::CurDir))
}

/// Find a static file using Django's staticfiles finders (for app-level static files)
fn find_with_django_finders(relative_path: &str) -> Option<PathBuf> {
    Python::attach(|py| {
        // Import the find_static_file function from django_bolt.admin.static
        let static_module = py.import("django_bolt.admin.static").ok()?;
        let find_fn = static_module.getattr("find_static_file").ok()?;

        // Call the Python function
        let result = find_fn.call1((relative_path,)).ok()?;

        // Extract the path string
        if result.is_none() {
            return None;
        }

        let path_str: String = result.extract().ok()?;
        Some(PathBuf::from(path_str))
    })
}

/// Give the canonical path of a file that a finder found, if the file stays
/// inside the root of the finder location that found it.
///
/// A finder joins the request path to the absolute path of its location, so
/// that location is a prefix of `found`. When locations nest, the deepest
/// prefix is the strictest choice, so Bolt checks against it. A symlink can
/// point out of the location. Thus the canonical file must start with the
/// canonical root. The handler opens the canonical path that we checked.
fn contain_finder_result(found: &Path, roots: &[FinderRoot]) -> Option<PathBuf> {
    let root = roots
        .iter()
        .filter(|root| found.starts_with(&root.location))
        .max_by_key(|root| root.location.components().count())?;
    let canonical = found.canonicalize().ok()?;
    let inside = match root.canonical {
        Some(ref root_canonical) => canonical.starts_with(root_canonical),
        // The directory was made after startup. Resolve it now.
        None => root
            .location
            .canonicalize()
            .is_ok_and(|root_canonical| canonical.starts_with(root_canonical)),
    };
    (inside && canonical.is_file()).then_some(canonical)
}

/// Read the roots of the Django staticfiles finders, once at startup.
///
/// Python gives the absolute path of each finder location. A location that
/// does not exist yet keeps no canonical form; a request resolves it if a
/// finder later finds a file there. A finder without a
/// filesystem storage has no known root, so Bolt refuses its files and
/// prints a warning.
pub fn load_finder_roots(py: Python<'_>) -> PyResult<Vec<FinderRoot>> {
    let (locations, unknown): (Vec<String>, Vec<String>) = py
        .import("django_bolt.admin.static")?
        .getattr("finder_locations")?
        .call0()?
        .extract()?;
    for finder in &unknown {
        eprintln!(
            "[django-bolt] Warning: Static files: the finder {finder} has no filesystem \
             storage. Bolt does not serve its files."
        );
    }
    Ok(locations
        .into_iter()
        .map(|location| {
            let location = PathBuf::from(location);
            let canonical = location.canonicalize().ok();
            FinderRoot {
                location,
                canonical,
            }
        })
        .collect())
}

/// Unified static/media file handler. Behaviour is driven entirely by
/// `config` (`ScopeConfig`), so both the `/static` and `/media` scopes route
/// here — there is no per-scope code path.
///
/// Uses actix-files `NamedFile` for streaming, ETag/Last-Modified, conditional
/// requests (304), range support, and content-type detection. Every response
/// carries `X-Content-Type-Options: nosniff` and (if configured) the startup-
/// built CSP header; dotfile components (`.env`, `.git/...`, …) are 404'd.
///
/// Scope-specific behaviour, all keyed off `config`:
/// - Static (`django_finders`, debug only): falls back to Django's
///   staticfiles finders for app static like admin. A found file must stay
///   inside the root of its finder location. In production only the
///   configured dirs (STATIC_ROOT, STATICFILES_DIRS) are served.
/// - Media (`ServeMode::Media`): never uses finders (they don't know
///   MEDIA_ROOT). A file whose content type can run script (HTML, XML,
///   JavaScript, WebAssembly) is a download as `application/octet-stream`.
///   `nosniff` alone does not stop a browser that obeys a `text/html` or
///   `image/svg+xml` type and runs the script. See `can_run_script`.
pub async fn handle_file(
    req: HttpRequest,
    path: web::Path<String>,
    config: web::Data<Arc<ScopeConfig>>,
) -> HttpResponse {
    // Strip leading slash if present (route captures include it)
    let relative_path = path.into_inner();
    let relative_path = relative_path.trim_start_matches('/');

    // Security headers (nosniff, CSP) attach to ALL responses so a 404 for
    // /media/<crafted> can't be MIME-sniffed into HTML/JS execution.
    //
    // Cache-Control is gated separately on success status (see
    // `apply_freshness_header`) — caching a 404 with a long max-age would
    // make a missing file invisible for the cache lifetime even after upload.
    let apply_security_headers = |response: &mut HttpResponse| {
        let headers = response.headers_mut();
        headers.insert(
            header::X_CONTENT_TYPE_OPTIONS,
            header::HeaderValue::from_static("nosniff"),
        );
        if let Some(ref csp) = config.csp_header {
            // `csp` is a pre-validated HeaderValue from startup; clone is
            // Bytes-backed and ~1ns.
            headers.insert(header::CONTENT_SECURITY_POLICY, csp.clone());
        }
    };
    let apply_freshness_header = |response: &mut HttpResponse| {
        // nginx's `expires` directive only fires on 200. We do the same —
        // 404/4xx caching forces a stale view of the resource long after the
        // file finally lands. 304 already carries the original 200's
        // Cache-Control via the client's cached entry, so no action needed.
        if response.status().is_success()
            && let Some(ref cc) = config.cache_control
        {
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, cc.clone());
        }
    };

    if relative_path.contains("..") {
        let mut response = HttpResponse::BadRequest()
            .content_type("text/plain; charset=utf-8")
            .body("Invalid path");
        apply_security_headers(&mut response);
        return response;
    }

    // Dotfile deny: return 404 (not 400) so probing /static/.env doesn't
    // confirm the prefix is configured. Same shape as a missing file.
    if has_dotfile_component(relative_path) {
        let mut response = not_found_response();
        apply_security_headers(&mut response);
        return response;
    }

    // The path to open, and the requested path whose name gives the content
    // type. A symlink keeps the type of its own name, not of its target.
    let mut file_path: Option<(PathBuf, PathBuf)> =
        find_in_directories(relative_path, &config.directories);

    // Static-only: fall back to Django finders in debug mode for app static
    // files like admin. Media intentionally skips this — finders only know
    // about STATICFILES_DIRS / app static dirs, never MEDIA_ROOT.
    // A finder result keeps the requested name for its content type, as
    // before the containment check.
    if file_path.is_none()
        && let Some(ref roots) = config.django_finders
    {
        file_path = find_with_django_finders(relative_path).and_then(|found| {
            contain_finder_result(&found, roots).map(|canonical| (canonical, found))
        });
    }

    let mut response = if let Some((ref resolved, ref name)) = file_path {
        serve_file(&req, resolved, name, config.mode).await
    } else {
        not_found_response()
    };
    apply_security_headers(&mut response);
    apply_freshness_header(&mut response);
    response
}

/// Open `file_path`, and take the content type from the name of `name`.
///
/// A media file that a browser can run as script goes out as
/// `application/octet-stream` with `Content-Disposition: attachment`. The
/// browser then saves the bytes and does not show them. Static files keep
/// their type: they are the files of the site. `NamedFile` puts the same
/// headers on a `200`, a `206`, a `304` and a `HEAD` response.
///
/// The disposition has no `filename` parameter, so the URL path does not go
/// into a header. The browser takes the name from the URL.
async fn serve_file(
    req: &HttpRequest,
    file_path: &Path,
    name: &Path,
    mode: ServeMode,
) -> HttpResponse {
    // Use sync reads for files under 256KB (faster for typical static assets)
    // See: https://github.com/actix/actix-web/pull/3706
    match std::fs::File::open(file_path).and_then(|file| NamedFile::from_file(file, name)) {
        Ok(mut named) => {
            if matches!(mode, ServeMode::Media) && media_can_run_script(&named, file_path, name) {
                named = named
                    .set_content_type(mime::APPLICATION_OCTET_STREAM)
                    .set_content_disposition(ContentDisposition {
                        disposition: DispositionType::Attachment,
                        parameters: Vec::new(),
                    });
            }
            named.read_mode_threshold(256 * 1024).into_response(req)
        }
        Err(_) => not_found_response(),
    }
}

/// True when the type of the response, or the type of the name of a symlink
/// target, can run script. The target name is checked only when it differs.
fn media_can_run_script(named: &NamedFile, file_path: &Path, name: &Path) -> bool {
    can_run_script(named.content_type())
        || (file_path.file_name() != name.file_name()
            && file_path
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| can_run_script(&file_extension_to_mime(ext))))
}

fn not_found_response() -> HttpResponse {
    HttpResponse::NotFound()
        .content_type("text/plain; charset=utf-8")
        .body("File not found")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, File};
    use std::io::Write;
    use tempfile::TempDir;

    /// The canonical path that the fast path opens.
    fn find_canonical(relative_path: &str, directories: &[ServeDir]) -> Option<PathBuf> {
        find_in_directories(relative_path, directories).map(|(canonical, _)| canonical)
    }

    /// Canonicalize as the server does at startup. The tests pass
    /// pre-canonicalized roots so they exercise the same fast path
    /// production uses (no per-request canonicalize() on the directory).
    fn canon(dir: &Path) -> PathBuf {
        dir.canonicalize().expect("test dir must canonicalize")
    }

    #[test]
    fn test_find_in_directories() {
        let temp_dir = TempDir::new().unwrap();
        let temp_path = temp_dir.path();

        // Create a test file
        let css_dir = temp_path.join("css");
        fs::create_dir(&css_dir).unwrap();
        let mut file = File::create(css_dir.join("style.css")).unwrap();
        file.write_all(b"body { color: red; }").unwrap();

        let directories = vec![ServeDir::at_root(canon(temp_path))];

        // Should find existing file
        let result = find_canonical("css/style.css", &directories);
        assert!(result.is_some());

        // Should not find non-existent file
        let result = find_canonical("css/missing.css", &directories);
        assert!(result.is_none());

        // Should reject directory traversal
        let result = find_canonical("../etc/passwd", &directories);
        assert!(result.is_none());

        // Should reject absolute paths
        let result = find_canonical("/etc/passwd", &directories);
        assert!(result.is_none());
    }

    #[test]
    fn test_find_in_directories_rejects_dot_dot_and_absolute_paths_inside_the_root() {
        // These paths name a real file inside the root, so the canonical
        // root check alone would serve them. The path check must reject them.
        let temp_dir = TempDir::new().unwrap();
        let root = canon(temp_dir.path());
        fs::create_dir(root.join("css")).unwrap();
        File::create(root.join("app.js")).unwrap();
        let directories = vec![ServeDir::at_root(root.clone())];

        assert!(find_canonical("app.js", &directories).is_some());
        assert!(find_canonical("css/../app.js", &directories).is_none());
        let absolute = root.join("app.js");
        assert!(find_canonical(absolute.to_str().unwrap(), &directories).is_none());
    }

    fn prefixed(prefix: &str, root: &Path) -> ServeDir {
        ServeDir {
            prefix: Some(format!("{prefix}/")),
            root: canon(root),
        }
    }

    /// A `(prefix, path)` entry serves its files under `prefix/` only (#944).
    #[test]
    fn prefixed_directory_serves_only_under_its_prefix() {
        let temp_dir = TempDir::new().unwrap();
        let vendor = temp_dir.path().join("vendor");
        fs::create_dir_all(vendor.join("css")).unwrap();
        fs::write(vendor.join("app.css"), "app").unwrap();
        fs::write(vendor.join("css/site.css"), "site").unwrap();
        let directories = vec![prefixed("vendor", &vendor)];

        assert_eq!(
            find_canonical("vendor/app.css", &directories),
            Some(canon(&vendor.join("app.css")))
        );
        assert_eq!(
            find_canonical("vendor/css/site.css", &directories),
            Some(canon(&vendor.join("css/site.css")))
        );
        assert!(find_canonical("app.css", &directories).is_none());
        assert!(find_canonical("vendorapp.css", &directories).is_none());
        assert!(find_canonical("vendor", &directories).is_none());
        assert!(find_canonical("vendor/", &directories).is_none());
        assert!(find_canonical("vendor/css", &directories).is_none());
        assert!(find_canonical("vendor/missing.css", &directories).is_none());
    }

    /// The rest of the path after the prefix must be a plain relative path.
    /// A join with an absolute rest would leave the directory.
    #[test]
    fn prefixed_directory_rejects_an_absolute_rest() {
        let temp_dir = TempDir::new().unwrap();
        let vendor = temp_dir.path().join("vendor");
        fs::create_dir(&vendor).unwrap();
        fs::write(vendor.join("app.css"), "app").unwrap();
        let absolute = canon(&vendor.join("app.css"));
        let directories = vec![prefixed("vendor", &vendor)];

        let request = format!("vendor/{}", absolute.to_str().unwrap());
        assert!(find_canonical(&request, &directories).is_none());
        assert!(find_canonical("vendor/../vendor/app.css", &directories).is_none());
    }

    #[cfg(unix)]
    #[test]
    fn prefixed_directory_refuses_a_symlink_out_of_its_root() {
        let temp_dir = TempDir::new().unwrap();
        let vendor = temp_dir.path().join("vendor");
        fs::create_dir(&vendor).unwrap();
        fs::write(temp_dir.path().join("secret.txt"), "secret").unwrap();
        std::os::unix::fs::symlink(temp_dir.path().join("secret.txt"), vendor.join("leak.txt"))
            .unwrap();
        let directories = vec![prefixed("vendor", &vendor)];

        assert!(find_canonical("vendor/leak.txt", &directories).is_none());
    }

    /// Plain and prefixed entries keep their order, as in Django.
    #[test]
    fn prefixed_and_plain_directories_are_searched_in_order() {
        let first = TempDir::new().unwrap();
        let second = TempDir::new().unwrap();
        fs::write(first.path().join("app.css"), "first").unwrap();
        fs::write(second.path().join("app.css"), "second").unwrap();
        let directories = vec![
            prefixed("vendor", first.path()),
            ServeDir::at_root(canon(second.path())),
            prefixed("vendor", second.path()),
        ];

        assert_eq!(
            find_canonical("vendor/app.css", &directories),
            Some(canon(&first.path().join("app.css")))
        );
        assert_eq!(
            find_canonical("app.css", &directories),
            Some(canon(&second.path().join("app.css")))
        );
    }

    fn scope(root: &Path, mode: ServeMode, finders: bool) -> ScopeConfig {
        ScopeConfig {
            url_prefix: "/files".to_string(),
            directories: vec![ServeDir::at_root(canon(root))],
            csp_header: None,
            cache_control: Some(header::HeaderValue::from_static("max-age=60")),
            mode,
            django_finders: finders.then(Vec::new),
        }
    }

    async fn get(config: ScopeConfig, name: &str) -> HttpResponse {
        let request = actix_web::test::TestRequest::default().to_http_request();
        let path = web::Path::from(name.to_string());
        handle_file(request, path, web::Data::new(Arc::new(config))).await
    }

    fn header_of<'a>(response: &'a HttpResponse, name: &str) -> Option<&'a str> {
        response
            .headers()
            .get(name)
            .map(|value| value.to_str().unwrap())
    }

    /// A browser must download an upload that can carry script, not render it.
    #[actix_web::test]
    async fn media_serves_an_upload_that_can_carry_script_as_a_download() {
        let temp_dir = TempDir::new().unwrap();
        fs::write(
            temp_dir.path().join("page.html"),
            "<script>alert(1)</script>",
        )
        .unwrap();
        fs::write(temp_dir.path().join("notes.txt"), "plain").unwrap();
        let media = || scope(temp_dir.path(), ServeMode::Media, false);

        let page = get(media(), "page.html").await;
        assert_eq!(page.status(), 200);
        assert_eq!(
            header_of(&page, "content-type"),
            Some("application/octet-stream")
        );
        assert_eq!(header_of(&page, "content-disposition"), Some("attachment"));
        assert_eq!(header_of(&page, "x-content-type-options"), Some("nosniff"));
        assert_eq!(header_of(&page, "cache-control"), Some("max-age=60"));

        let notes = get(media(), "notes.txt").await;
        assert_eq!(notes.status(), 200);
        assert_eq!(
            header_of(&notes, "content-type"),
            Some("text/plain; charset=utf-8")
        );
        assert_ne!(header_of(&notes, "content-disposition"), Some("attachment"));
    }

    /// The content type comes from the requested name. Thus a media symlink
    /// with a script name must download, also when its target has a safe
    /// name. A symlink to a script file must download too.
    #[cfg(unix)]
    #[actix_web::test]
    async fn media_symlink_with_a_script_name_or_target_is_a_download() {
        let temp_dir = TempDir::new().unwrap();
        let root = temp_dir.path();
        fs::write(root.join("notes.txt"), "<script>alert(1)</script>").unwrap();
        fs::write(root.join("page.html"), "<script>alert(1)</script>").unwrap();
        std::os::unix::fs::symlink(root.join("notes.txt"), root.join("evil.html")).unwrap();
        std::os::unix::fs::symlink(root.join("page.html"), root.join("alias.txt")).unwrap();
        let media = || scope(root, ServeMode::Media, false);

        for name in ["evil.html", "alias.txt"] {
            let response = get(media(), name).await;
            assert_eq!(response.status(), 200, "{name}");
            assert_eq!(
                header_of(&response, "content-type"),
                Some("application/octet-stream"),
                "{name}"
            );
            assert_eq!(
                header_of(&response, "content-disposition"),
                Some("attachment"),
                "{name}"
            );
        }
    }

    /// A static symlink keeps the content type of its own name, as a file
    /// from the finders does.
    #[cfg(unix)]
    #[actix_web::test]
    async fn static_symlink_keeps_the_type_of_the_requested_name() {
        let temp_dir = TempDir::new().unwrap();
        let root = temp_dir.path();
        fs::write(root.join("payload.txt"), "body{}").unwrap();
        std::os::unix::fs::symlink(root.join("payload.txt"), root.join("style.css")).unwrap();

        let response = get(scope(root, ServeMode::Static, false), "style.css").await;
        assert_eq!(response.status(), 200);
        assert_eq!(
            header_of(&response, "content-type"),
            Some("text/css; charset=utf-8")
        );
    }

    #[actix_web::test]
    async fn static_keeps_the_type_of_a_file_and_does_not_cache_a_404() {
        // The finders of Django run in Python. Here they find no file.
        Python::initialize();
        let temp_dir = TempDir::new().unwrap();
        fs::write(temp_dir.path().join("page.html"), "<p>docs</p>").unwrap();
        let files = || scope(temp_dir.path(), ServeMode::Static, true);

        let page = get(files(), "page.html").await;
        assert_eq!(page.status(), 200);
        assert_eq!(
            header_of(&page, "content-type"),
            Some("text/html; charset=utf-8")
        );
        assert_ne!(header_of(&page, "content-disposition"), Some("attachment"));

        let missing = get(files(), "missing.css").await;
        assert_eq!(missing.status(), 404);
        assert_eq!(
            header_of(&missing, "x-content-type-options"),
            Some("nosniff")
        );
        assert_eq!(header_of(&missing, "cache-control"), None);
    }

    fn finder_root(location: &Path) -> FinderRoot {
        FinderRoot {
            location: location.to_path_buf(),
            canonical: Some(canon(location)),
        }
    }

    #[cfg(unix)]
    #[test]
    fn finder_result_must_stay_inside_its_root() {
        let temp_dir = TempDir::new().unwrap();
        let root = temp_dir.path().join("static");
        let other = temp_dir.path().join("other");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&other).unwrap();
        fs::write(root.join("app.css"), "app").unwrap();
        fs::write(other.join("other.css"), "other").unwrap();
        fs::write(temp_dir.path().join("secret.txt"), "secret").unwrap();
        std::os::unix::fs::symlink(temp_dir.path().join("secret.txt"), root.join("leak.txt"))
            .unwrap();
        std::os::unix::fs::symlink(root.join("app.css"), root.join("alias.css")).unwrap();
        // A link into another finder root also leaves the root that found it.
        std::os::unix::fs::symlink(other.join("other.css"), root.join("cross.css")).unwrap();
        fs::create_dir(root.join("dir")).unwrap();
        let roots = [finder_root(&root), finder_root(&other)];

        assert_eq!(
            contain_finder_result(&root.join("app.css"), &roots),
            Some(canon(&root.join("app.css")))
        );
        assert_eq!(
            contain_finder_result(&root.join("alias.css"), &roots),
            Some(canon(&root.join("app.css")))
        );
        assert_eq!(contain_finder_result(&root.join("leak.txt"), &roots), None);
        assert_eq!(contain_finder_result(&root.join("cross.css"), &roots), None);
        assert_eq!(contain_finder_result(&root.join("dir"), &roots), None);
        assert_eq!(
            contain_finder_result(&root.join("missing.css"), &roots),
            None
        );
        // A path from no known root (a finder without a storage) is refused.
        assert_eq!(contain_finder_result(&root.join("app.css"), &[]), None);
    }

    /// Nested locations: a link may not leave the deepest location that
    /// holds the found path, also when an outer location holds the target.
    #[cfg(unix)]
    #[test]
    fn nested_finder_root_is_checked_against_the_deepest_location() {
        let temp_dir = TempDir::new().unwrap();
        let outer = temp_dir.path().join("assets");
        let inner = outer.join("vendor");
        fs::create_dir_all(&inner).unwrap();
        fs::write(outer.join(".env"), "secret").unwrap();
        fs::write(inner.join("app.css"), "app").unwrap();
        std::os::unix::fs::symlink(outer.join(".env"), inner.join("leak.txt")).unwrap();
        let roots = [finder_root(&outer), finder_root(&inner)];

        assert_eq!(contain_finder_result(&inner.join("leak.txt"), &roots), None);
        assert_eq!(
            contain_finder_result(&inner.join("app.css"), &roots),
            Some(canon(&inner.join("app.css")))
        );
        assert_eq!(
            contain_finder_result(&outer.join(".env"), &roots),
            Some(canon(&outer.join(".env")))
        );
    }

    /// A finder location that is itself a symlink to a directory is legal.
    /// Its files resolve into the target, which is the canonical root.
    #[cfg(unix)]
    #[test]
    fn finder_root_that_is_a_symlink_serves_its_files() {
        let temp_dir = TempDir::new().unwrap();
        let real = temp_dir.path().join("real");
        fs::create_dir(&real).unwrap();
        fs::write(real.join("app.css"), "app").unwrap();
        fs::write(temp_dir.path().join("secret.txt"), "secret").unwrap();
        std::os::unix::fs::symlink(temp_dir.path().join("secret.txt"), real.join("leak.txt"))
            .unwrap();
        let linked = temp_dir.path().join("linked");
        std::os::unix::fs::symlink(&real, &linked).unwrap();
        let roots = [finder_root(&linked)];

        assert_eq!(
            contain_finder_result(&linked.join("app.css"), &roots),
            Some(canon(&real.join("app.css")))
        );
        assert_eq!(
            contain_finder_result(&linked.join("leak.txt"), &roots),
            None
        );
    }

    /// A finder location made after startup has no canonical form yet.
    #[cfg(unix)]
    #[test]
    fn finder_root_made_after_startup_is_resolved_on_request() {
        let temp_dir = TempDir::new().unwrap();
        let later = temp_dir.path().join("later");
        let roots = [FinderRoot {
            location: later.clone(),
            canonical: None,
        }];
        fs::create_dir(&later).unwrap();
        fs::write(later.join("app.css"), "app").unwrap();
        fs::write(temp_dir.path().join("secret.txt"), "secret").unwrap();
        std::os::unix::fs::symlink(temp_dir.path().join("secret.txt"), later.join("leak.txt"))
            .unwrap();

        assert_eq!(
            contain_finder_result(&later.join("app.css"), &roots),
            Some(canon(&later.join("app.css")))
        );
        assert_eq!(contain_finder_result(&later.join("leak.txt"), &roots), None);
    }

    #[test]
    fn test_is_plain_relative() {
        assert!(is_plain_relative("css/app.js"));
        assert!(is_plain_relative("./app.js"));
        assert!(!is_plain_relative("/etc/passwd"));
        assert!(!is_plain_relative("css/../app.js"));
    }

    /// A drive or UNC prefix replaces the root in `join` on Windows.
    #[cfg(windows)]
    #[test]
    fn test_is_plain_relative_rejects_windows_prefixes() {
        assert!(!is_plain_relative(r"C:\Windows\win.ini"));
        assert!(!is_plain_relative("C:/Windows/win.ini"));
        assert!(!is_plain_relative("C:win.ini"));
        assert!(!is_plain_relative(r"\\server\share\file"));
        assert!(!is_plain_relative(r"\Windows\win.ini"));
    }

    #[test]
    fn test_find_in_multiple_directories() {
        let dir1 = TempDir::new().unwrap();
        let dir2 = TempDir::new().unwrap();

        // Create file only in dir1
        let mut file1 = File::create(dir1.path().join("file1.txt")).unwrap();
        file1.write_all(b"content1").unwrap();

        // Create file only in dir2
        let mut file2 = File::create(dir2.path().join("file2.txt")).unwrap();
        file2.write_all(b"content2").unwrap();

        let directories = vec![
            ServeDir::at_root(canon(dir1.path())),
            ServeDir::at_root(canon(dir2.path())),
        ];

        // Should find file1 in dir1
        let result = find_canonical("file1.txt", &directories);
        assert!(result.is_some());
        assert!(result.unwrap().to_string_lossy().contains("file1.txt"));

        // Should find file2 in dir2
        let result = find_canonical("file2.txt", &directories);
        assert!(result.is_some());
        assert!(result.unwrap().to_string_lossy().contains("file2.txt"));
    }

    fn mime(essence: &str) -> Mime {
        essence.parse().unwrap()
    }

    #[test]
    fn html_xml_javascript_and_wasm_types_can_run_script() {
        for essence in [
            "text/html",
            "text/html; charset=utf-8",
            "TEXT/HTML",
            "text/xml",
            "application/xml",
            "application/xhtml+xml",
            "image/svg+xml",
            "IMAGE/SVG+XML",
            "application/rss+xml",
            "application/atom+xml",
            "application/rdf+xml",
            "application/mathml+xml",
            "application/xslt+xml",
            "model/x3d+xml",
            "application/ecmascript",
            "application/javascript",
            "application/x-ecmascript",
            "application/x-javascript",
            "text/ecmascript",
            "text/javascript",
            "text/javascript1.0",
            "text/javascript1.5",
            "text/jscript",
            "text/livescript",
            "text/x-ecmascript",
            "text/x-javascript",
            "application/wasm",
            "text/x-component",
            "application/hta",
        ] {
            assert!(can_run_script(&mime(essence)), "{essence}");
        }
        for essence in [
            "image/png",
            "image/jpeg",
            "application/pdf",
            "text/plain",
            "text/css",
            "application/json",
            "application/octet-stream",
            "application/xml-dtd",
            "application/wbxml",
            "text/javascript2.0",
            "video/mp4",
        ] {
            assert!(!can_run_script(&mime(essence)), "{essence}");
        }
    }

    /// Walk each extension that mime_guess knows. A media file of a type that
    /// can run script must be a download, and any other file keeps its type.
    /// A new mime_guess release that maps a new extension to such a type
    /// thus cannot serve that file inline.
    #[actix_web::test]
    async fn every_media_extension_with_a_script_type_is_a_download() {
        let temp_dir = TempDir::new().unwrap();
        let extensions: std::collections::BTreeSet<&str> = mime_guess::get_extensions("*", "*")
            .unwrap()
            .iter()
            .copied()
            .collect();
        let mut downloads = std::collections::BTreeSet::new();
        for ext in &extensions {
            let file_name = format!("upload.{ext}");
            fs::write(temp_dir.path().join(&file_name), "<script>1</script>").unwrap();
            let sent = mime_guess::from_path(&file_name).first_or_octet_stream();
            let response = get(scope(temp_dir.path(), ServeMode::Media, false), &file_name).await;
            assert_eq!(response.status(), 200, "{file_name}");
            let content_type = header_of(&response, "content-type").unwrap();
            if can_run_script(&sent) {
                assert_eq!(content_type, "application/octet-stream", "{file_name}");
                assert_eq!(
                    header_of(&response, "content-disposition"),
                    Some("attachment"),
                    "{file_name}"
                );
                downloads.insert(*ext);
            } else {
                assert!(content_type.starts_with(sent.essence_str()), "{file_name}");
            }
        }
        // The ten extensions of audit finding M1 and the common script types.
        for ext in [
            "xht", "rss", "atom", "rdf", "mathml", "xsd", "dtd", "es", "jsm", "ecma", "html",
            "htm", "xhtml", "shtml", "htc", "hta", "svg", "svgz", "xml", "xsl", "xslt", "js",
            "mjs", "wasm",
        ] {
            assert!(downloads.contains(ext), "{ext}");
        }
        assert!(downloads.len() > 150, "{}", downloads.len());
    }

    /// An extension that mime_guess does not know, or no extension, gives
    /// `application/octet-stream`. A browser saves such a file.
    #[actix_web::test]
    async fn media_of_an_unknown_type_is_an_octet_stream_download() {
        let temp_dir = TempDir::new().unwrap();
        for name in ["upload.xhtm", "upload.shtm", "upload.cjs", "upload"] {
            fs::write(temp_dir.path().join(name), "<script>1</script>").unwrap();
            let response = get(scope(temp_dir.path(), ServeMode::Media, false), name).await;
            assert_eq!(
                header_of(&response, "content-type"),
                Some("application/octet-stream"),
                "{name}"
            );
            assert!(
                header_of(&response, "content-disposition")
                    .unwrap()
                    .starts_with("attachment"),
                "{name}"
            );
        }
    }

    /// Static files are the files of the site, so each keeps its own type.
    #[actix_web::test]
    async fn static_files_of_a_script_type_keep_their_type() {
        let temp_dir = TempDir::new().unwrap();
        for (name, essence) in [
            ("page.xht", "application/xhtml+xml"),
            ("feed.rss", "application/rss+xml"),
            ("app.js", "text/javascript"),
            ("logo.svg", "image/svg+xml"),
        ] {
            fs::write(temp_dir.path().join(name), "<script>1</script>").unwrap();
            let response = get(scope(temp_dir.path(), ServeMode::Static, false), name).await;
            assert!(
                header_of(&response, "content-type")
                    .unwrap()
                    .starts_with(essence),
                "{name}"
            );
        }
    }

    #[test]
    fn test_has_dotfile_component() {
        // Leaf dotfile.
        assert!(has_dotfile_component(".env"));
        assert!(has_dotfile_component(".htaccess"));
        // Nested dotfile or dot-directory.
        assert!(has_dotfile_component("subdir/.env"));
        assert!(has_dotfile_component(".git/config"));
        assert!(has_dotfile_component(".ssh/id_rsa"));
        // Backslash separator (Windows-style traversal/component).
        assert!(has_dotfile_component("foo\\.env"));
        assert!(has_dotfile_component(".git\\config"));
        // Normal paths must not be flagged.
        assert!(!has_dotfile_component("css/style.css"));
        assert!(!has_dotfile_component("photos/img.png"));
        assert!(!has_dotfile_component("a/b/c.txt"));
        // A `.` in the middle of a filename is fine — only leading dot counts.
        assert!(!has_dotfile_component("file.name.txt"));
        assert!(!has_dotfile_component("v1.2.3/build"));
    }

    #[test]
    fn test_directory_priority() {
        let dir1 = TempDir::new().unwrap();
        let dir2 = TempDir::new().unwrap();

        // Create same-named file in both directories
        let mut file1 = File::create(dir1.path().join("shared.txt")).unwrap();
        file1.write_all(b"from_dir1").unwrap();

        let mut file2 = File::create(dir2.path().join("shared.txt")).unwrap();
        file2.write_all(b"from_dir2").unwrap();

        // dir1 should take priority (listed first)
        let directories = vec![
            ServeDir::at_root(canon(dir1.path())),
            ServeDir::at_root(canon(dir2.path())),
        ];

        let result = find_canonical("shared.txt", &directories);
        assert!(result.is_some());

        // Verify it's from dir1
        let content = fs::read_to_string(result.unwrap()).unwrap();
        assert_eq!(content, "from_dir1");
    }
}
