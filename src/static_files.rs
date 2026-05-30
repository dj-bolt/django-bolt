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
use actix_web::{http::header, web, HttpRequest, HttpResponse};
use pyo3::prelude::*;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::state::{ScopeConfig, ServeMode};

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
    "html", "htm", "xhtml", "xhtm", "shtml", "shtm", "htc", "hta",
    "svg", "svgz",
    "xml", "xsl", "xslt",
    "js", "mjs", "cjs",
    "wasm",
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
    // Security: prevent directory traversal
    if relative_path.contains("..") || relative_path.starts_with('/') {
        return None;
    }

    for dir_canonical in directories {
        let full_path = dir_canonical.join(relative_path);

        // Canonicalize the *file* (not the dir) to resolve any symlinks in
        // the relative portion, then verify the result is still inside the
        // canonical root. Stops a symlink-out-of-root from escaping.
        if let Ok(canonical) = full_path.canonicalize() {
            if canonical.starts_with(dir_canonical) && canonical.is_file() {
                return Some(canonical);
            }
        }
    }
    None
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
/// - Static (`allow_django_finders`, debug only): falls back to Django's
///   staticfiles finders for app static like admin. In production only the
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
        if response.status().is_success() {
            if let Some(ref cc) = config.cache_control {
                response
                    .headers_mut()
                    .insert(header::CACHE_CONTROL, cc.clone());
            }
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

    let mut file_path = find_in_directories(relative_path, &config.directories);

    // Static-only: fall back to Django finders in debug mode for app static
    // files like admin. Media intentionally skips this — finders only know
    // about STATICFILES_DIRS / app static dirs, never MEDIA_ROOT.
    if config.allow_django_finders && file_path.is_none() {
        file_path = find_with_django_finders(relative_path);
    }

    let mut response = if let Some(ref resolved) = file_path {
        let mut r = serve_file(&req, resolved).await;
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

async fn serve_file(req: &HttpRequest, file_path: &Path) -> HttpResponse {
    // Use sync reads for files under 256KB (faster for typical static assets)
    // See: https://github.com/actix/actix-web/pull/3706
    match NamedFile::open_async(file_path).await {
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
            "html", "HTM", "xhtml", "xhtm", "shtml", "shtm", "htc", "hta",
            "svg", "SVGZ",
            "xml", "xsl", "xslt",
            "js", "MJS", "cjs",
            "wasm",
        ] {
            let p = Path::new(&format!("upload.{}", ext)).to_path_buf();
            assert!(
                is_dangerous_media_ext(&p),
                "{} must be flagged dangerous (case-insensitive)",
                ext
            );
        }
        // Inert types stay inline so legitimate avatars/images keep working.
        for ext in &["png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "json", "css"] {
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
