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

use actix_files::NamedFile;
use actix_web::{HttpRequest, HttpResponse, http::header, web};
use pyo3::prelude::*;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use crate::state::{FinderRoot, ScopeConfig, ServeMode};

/// Extensions that can carry executable script in a browser context.
/// For media (user uploads), these are rewritten to `application/octet-stream`
/// + `Content-Disposition: attachment` so they cannot run in the site's origin.
///
/// Picked for what *actually executes JS in your origin* when fetched directly:
/// HTML-family (rendered), SVG-family (script-capable), XML/XSLT (XHTML
/// rendering, XSLT can JS), JS-family (loaded as `<script>`), WASM (loaded by
/// JS, but a known origin-confused-deputy vector). CSS is omitted: it can
/// exfiltrate but cannot execute, and forcing CSS to download would be
/// gratuitous for the common "let users upload theme overrides" case.
const DANGEROUS_MEDIA_EXTS: &[&str] = &[
    "html", "htm", "xhtml", "xhtm", "shtml", "shtm", "htc", "hta", "svg", "svgz", "xml", "xsl",
    "xslt", "js", "mjs", "cjs", "wasm",
];

fn is_dangerous_media_ext(path: &Path) -> bool {
    let Some(ext) = path.extension().and_then(|e| e.to_str()) else {
        return false;
    };
    // Hot path: zero-allocation comparison. `to_ascii_lowercase()` would
    // heap-allocate a String for every media request.
    DANGEROUS_MEDIA_EXTS
        .iter()
        .any(|candidate| candidate.eq_ignore_ascii_case(ext))
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
fn find_in_directories(relative_path: &str, directories: &[PathBuf]) -> Option<PathBuf> {
    // Security: prevent directory traversal. A path of plain names only: no
    // `..`, no root (`/etc`) and, on Windows, no drive or UNC prefix (`C:\`).
    if relative_path.contains("..") || !is_plain_relative(relative_path) {
        return None;
    }

    for dir_canonical in directories {
        let full_path = dir_canonical.join(relative_path);

        // Canonicalize the *file* (not the dir) to resolve any symlinks in
        // the relative portion, then verify the result is still inside the
        // canonical root. Stops a symlink-out-of-root from escaping.
        if let Ok(canonical) = full_path.canonicalize()
            && canonical.starts_with(dir_canonical)
            && canonical.is_file()
        {
            return Some(canonical);
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
///   MEDIA_ROOT), and any upload whose extension can carry script
///   (`.html`, `.svg`, `.js`, `.wasm`, …) is force-downloaded as
///   `application/octet-stream` — `nosniff` alone won't stop a browser from
///   honouring a `text/html`/`image/svg+xml` type and running its scripts.
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

    // The path to open, and for a finder result the path whose name gives
    // the content type. `None` takes the type from the opened path.
    let mut file_path: Option<(PathBuf, Option<PathBuf>)> =
        find_in_directories(relative_path, &config.directories).map(|path| (path, None));

    // Static-only: fall back to Django finders in debug mode for app static
    // files like admin. Media intentionally skips this — finders only know
    // about STATICFILES_DIRS / app static dirs, never MEDIA_ROOT.
    // A finder result keeps the requested name for its content type, as
    // before the containment check.
    if file_path.is_none()
        && let Some(ref roots) = config.django_finders
    {
        file_path = find_with_django_finders(relative_path).and_then(|found| {
            contain_finder_result(&found, roots).map(|canonical| (canonical, Some(found)))
        });
    }

    let mut response = if let Some((ref resolved, ref name)) = file_path {
        let mut r = serve_file(&req, resolved, name.as_deref().unwrap_or(resolved)).await;
        // XSS disarm: rewrite Content-Type and force download for any media
        // upload whose extension can carry JS. Static keeps native types — a
        // CMS-served `theme.html` is admin-curated.
        if matches!(config.mode, ServeMode::Media) && is_dangerous_media_ext(resolved) {
            disarm_scripting_response(&mut r);
        }
        r
    } else {
        not_found_response()
    };
    apply_security_headers(&mut response);
    apply_freshness_header(&mut response);
    response
}

/// Open `file_path`, and take the content type from the name of `name`.
async fn serve_file(req: &HttpRequest, file_path: &Path, name: &Path) -> HttpResponse {
    // Use sync reads for files under 256KB (faster for typical static assets)
    // See: https://github.com/actix/actix-web/pull/3706
    match std::fs::File::open(file_path).and_then(|file| NamedFile::from_file(file, name)) {
        Ok(named) => named.read_mode_threshold(256 * 1024).into_response(req),
        Err(_) => not_found_response(),
    }
}

/// Replace the response's Content-Type with `application/octet-stream` and
/// force `Content-Disposition: attachment`. Together these tell every modern
/// browser to download the bytes rather than render them, which neutralises
/// stored XSS via user-uploaded HTML/SVG/JS in MEDIA_ROOT.
///
/// We deliberately do NOT include a `filename=` parameter to avoid feeding
/// attacker-controlled bytes (the URL path) into header construction. The
/// browser falls back to the last path segment of the URL for the saved
/// filename, which is fine.
fn disarm_scripting_response(response: &mut HttpResponse) {
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("application/octet-stream"),
    );
    headers.insert(
        header::CONTENT_DISPOSITION,
        header::HeaderValue::from_static("attachment"),
    );
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

        let directories = vec![canon(temp_path)];

        // Should find existing file
        let result = find_in_directories("css/style.css", &directories);
        assert!(result.is_some());

        // Should not find non-existent file
        let result = find_in_directories("css/missing.css", &directories);
        assert!(result.is_none());

        // Should reject directory traversal
        let result = find_in_directories("../etc/passwd", &directories);
        assert!(result.is_none());

        // Should reject absolute paths
        let result = find_in_directories("/etc/passwd", &directories);
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
        let directories = vec![root.clone()];

        assert!(find_in_directories("app.js", &directories).is_some());
        assert!(find_in_directories("css/../app.js", &directories).is_none());
        let absolute = root.join("app.js");
        assert!(find_in_directories(absolute.to_str().unwrap(), &directories).is_none());
    }

    fn scope(root: &Path, mode: ServeMode, finders: bool) -> ScopeConfig {
        ScopeConfig {
            url_prefix: "/files".to_string(),
            directories: vec![canon(root)],
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

        let directories = vec![canon(dir1.path()), canon(dir2.path())];

        // Should find file1 in dir1
        let result = find_in_directories("file1.txt", &directories);
        assert!(result.is_some());
        assert!(result.unwrap().to_string_lossy().contains("file1.txt"));

        // Should find file2 in dir2
        let result = find_in_directories("file2.txt", &directories);
        assert!(result.is_some());
        assert!(result.unwrap().to_string_lossy().contains("file2.txt"));
    }

    #[test]
    fn test_is_dangerous_media_ext() {
        // Script-bearing extensions must be flagged regardless of case.
        for ext in &[
            "html", "HTM", "xhtml", "xhtm", "shtml", "shtm", "htc", "hta", "svg", "SVGZ", "xml",
            "xsl", "xslt", "js", "MJS", "cjs", "wasm",
        ] {
            let p = Path::new(&format!("upload.{}", ext)).to_path_buf();
            assert!(
                is_dangerous_media_ext(&p),
                "{} must be flagged dangerous (case-insensitive)",
                ext
            );
        }
        // Inert types stay inline so legitimate avatars/images keep working.
        for ext in &[
            "png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "json", "css",
        ] {
            let p = Path::new(&format!("upload.{}", ext)).to_path_buf();
            assert!(
                !is_dangerous_media_ext(&p),
                "{} must NOT be flagged dangerous",
                ext
            );
        }
        // No extension at all: don't rewrite. Browsers default to octet-stream
        // for unknown types anyway via nosniff, so this is safe.
        assert!(!is_dangerous_media_ext(Path::new("Makefile")));
        assert!(!is_dangerous_media_ext(Path::new("")));
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
        let directories = vec![canon(dir1.path()), canon(dir2.path())];

        let result = find_in_directories("shared.txt", &directories);
        assert!(result.is_some());

        // Verify it's from dir1
        let content = fs::read_to_string(result.unwrap()).unwrap();
        assert_eq!(content, "from_dir1");
    }
}
