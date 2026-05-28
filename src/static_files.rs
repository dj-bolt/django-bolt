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

use crate::state::AppState;

/// Per-scope Cache-Control header, pre-validated as a `HeaderValue` at startup
/// from `BOLT_STATIC_MAX_AGE` / `BOLT_MEDIA_MAX_AGE`. Wrapped in a newtype so
/// the inner `Option<HeaderValue>` doesn't type-collide with other extractors
/// in the same scope.
#[derive(Clone, Debug)]
pub struct CacheControlHeader(pub Option<header::HeaderValue>);

/// Find a static file in the configured directories (fast path)
fn find_in_directories(relative_path: &str, directories: &[String]) -> Option<PathBuf> {
    // Security: prevent directory traversal
    if relative_path.contains("..") || relative_path.starts_with('/') {
        return None;
    }

    for dir in directories {
        let full_path = Path::new(dir).join(relative_path);

        // Verify the resolved path is still within the directory (prevent symlink attacks)
        if let Ok(canonical) = full_path.canonicalize() {
            if let Ok(dir_canonical) = Path::new(dir).canonicalize() {
                if canonical.starts_with(&dir_canonical) && canonical.is_file() {
                    return Some(canonical);
                }
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

/// Handler for static file requests
///
/// Uses actix-files NamedFile which provides:
/// - Streaming responses (memory efficient)
/// - Automatic ETag generation
/// - Last-Modified headers
/// - Conditional request handling (304 Not Modified)
/// - Range request support
/// - Content-Type detection
/// - CSP headers from Django settings (pre-built at server startup)
///
/// Security note:
/// - Django finders fallback (for app static files like admin) is only enabled in debug mode
/// - In production (DEBUG=False), only configured directories (STATIC_ROOT, STATICFILES_DIRS) are served
/// - This prevents potential path exposure from Django app finders in production
pub async fn handle_static_file(
    req: HttpRequest,
    path: web::Path<String>,
    directories: web::Data<Vec<String>>,
    csp_header: web::Data<Option<String>>,
    cache_control: web::Data<CacheControlHeader>,
    app_state: web::Data<Arc<AppState>>,
) -> HttpResponse {
    serve_request(
        req,
        path,
        directories,
        csp_header,
        cache_control,
        false, // nosniff
        Some(app_state),
    )
    .await
}

/// Handler for media file requests.
///
/// Same as `handle_static_file` but without the Django staticfiles finders
/// fallback — finders only know about STATICFILES_DIRS / app `static/` dirs,
/// never `MEDIA_ROOT`, so falling through would leak static assets under /media/.
/// Always emits `X-Content-Type-Options: nosniff` so user-uploaded HTML/SVG
/// can't be coerced into being rendered as HTML/JS by the browser.
pub async fn handle_media_file(
    req: HttpRequest,
    path: web::Path<String>,
    directories: web::Data<Vec<String>>,
    csp_header: web::Data<Option<String>>,
    cache_control: web::Data<CacheControlHeader>,
) -> HttpResponse {
    serve_request(req, path, directories, csp_header, cache_control, true, None).await
}

async fn serve_request(
    req: HttpRequest,
    path: web::Path<String>,
    directories: web::Data<Vec<String>>,
    csp_header: web::Data<Option<String>>,
    cache_control: web::Data<CacheControlHeader>,
    nosniff: bool,
    static_app_state: Option<web::Data<Arc<AppState>>>,
) -> HttpResponse {
    // Strip leading slash if present (route captures include it)
    let relative_path = path.into_inner();
    let relative_path = relative_path.trim_start_matches('/');

    // Headers are applied uniformly to success AND error responses so a 404
    // for /media/<crafted> can't be MIME-sniffed into HTML/JS execution.
    let apply_headers = |response: &mut HttpResponse| {
        if nosniff {
            response.headers_mut().insert(
                header::X_CONTENT_TYPE_OPTIONS,
                header::HeaderValue::from_static("nosniff"),
            );
        }
        if let Some(ref csp) = **csp_header {
            if let Ok(value) = header::HeaderValue::from_str(csp) {
                response
                    .headers_mut()
                    .insert(header::CONTENT_SECURITY_POLICY, value);
            }
        }
        if let Some(ref cc) = cache_control.0 {
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, cc.clone());
        }
    };

    if relative_path.contains("..") {
        let mut response = HttpResponse::BadRequest()
            .content_type("text/plain; charset=utf-8")
            .body("Invalid path");
        apply_headers(&mut response);
        return response;
    }

    let mut file_path = find_in_directories(relative_path, directories.as_ref());

    // Static-only: fall back to Django finders in debug mode for app static
    // files like admin. Media intentionally skips this — finders only know
    // about STATICFILES_DIRS / app static dirs, never MEDIA_ROOT.
    if let Some(app_state) = static_app_state {
        if file_path.is_none() && app_state.debug {
            file_path = find_with_django_finders(relative_path);
        }
    }

    let mut response = match file_path {
        Some(path) => serve_file(&req, &path).await,
        None => not_found_response(),
    };
    apply_headers(&mut response);
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

    #[test]
    fn test_find_in_directories() {
        let temp_dir = TempDir::new().unwrap();
        let temp_path = temp_dir.path();

        // Create a test file
        let css_dir = temp_path.join("css");
        fs::create_dir(&css_dir).unwrap();
        let mut file = File::create(css_dir.join("style.css")).unwrap();
        file.write_all(b"body { color: red; }").unwrap();

        let directories = vec![temp_path.to_string_lossy().to_string()];

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

        let directories = vec![
            dir1.path().to_string_lossy().to_string(),
            dir2.path().to_string_lossy().to_string(),
        ];

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
            dir1.path().to_string_lossy().to_string(),
            dir2.path().to_string_lossy().to_string(),
        ];

        let result = find_in_directories("shared.txt", &directories);
        assert!(result.is_some());

        // Verify it's from dir1
        let content = fs::read_to_string(result.unwrap()).unwrap();
        assert_eq!(content, "from_dir1");
    }
}
