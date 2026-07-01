//! Form parsing module for URL-encoded and multipart form data
//!
//! Provides Rust-native form parsing with type coercion and file validation,
//! eliminating Python GIL overhead for form-heavy endpoints.

use actix_multipart::Multipart;
use futures_util::StreamExt;
use std::collections::HashMap;
use std::io::Write;
use tempfile::NamedTempFile;

use crate::type_coercion::{coerce_param_with_limit, CoerceError, CoercedValue, TYPE_STRING};

/// Memory limit for in-memory file storage (1MB default)
pub const DEFAULT_MEMORY_LIMIT: usize = 1024 * 1024;

/// Maximum number of multipart parts allowed
pub const DEFAULT_MAX_PARTS: usize = 100;

/// File content - either in memory or spooled to disk
#[derive(Debug)]
pub enum FileContent {
    /// Small files kept in memory
    Memory(Vec<u8>),
    /// Large files spooled to temporary file on disk
    Disk(NamedTempFile),
}

/// File information with disk spooling support
#[derive(Debug)]
pub struct FileInfo {
    pub filename: String,
    pub content: FileContent,
    pub content_type: String,
    pub size: usize,
}

/// File field constraints for validation
#[derive(Debug, Clone, Default)]
pub struct FileFieldConstraints {
    pub max_size: Option<usize>,
    pub min_size: Option<usize>,
    pub allowed_types: Option<Vec<String>>,
    pub max_files: Option<usize>,
}

/// Validation error with detailed context
#[derive(Debug)]
pub struct ValidationError {
    pub error_type: String,
    pub loc: Vec<String>,
    pub msg: String,
    pub ctx: HashMap<String, serde_json::Value>,
}

impl ValidationError {
    pub fn file_too_large(field: &str, max_size: usize, actual_size: usize) -> Self {
        let mut ctx = HashMap::new();
        ctx.insert("max_size".to_string(), serde_json::json!(max_size));
        ctx.insert("actual_size".to_string(), serde_json::json!(actual_size));
        ValidationError {
            error_type: "file_too_large".to_string(),
            loc: vec!["body".to_string(), field.to_string()],
            msg: format!("File exceeds maximum size of {} bytes", max_size),
            ctx,
        }
    }

    pub fn file_too_small(field: &str, min_size: usize, actual_size: usize) -> Self {
        let mut ctx = HashMap::new();
        ctx.insert("min_size".to_string(), serde_json::json!(min_size));
        ctx.insert("actual_size".to_string(), serde_json::json!(actual_size));
        ValidationError {
            error_type: "file_too_small".to_string(),
            loc: vec!["body".to_string(), field.to_string()],
            msg: format!("File is below minimum size of {} bytes", min_size),
            ctx,
        }
    }

    pub fn invalid_content_type(field: &str, allowed_types: &[String], actual_type: &str) -> Self {
        let mut ctx = HashMap::new();
        ctx.insert(
            "allowed_types".to_string(),
            serde_json::json!(allowed_types),
        );
        ctx.insert("actual_type".to_string(), serde_json::json!(actual_type));
        ValidationError {
            error_type: "file_invalid_content_type".to_string(),
            loc: vec!["body".to_string(), field.to_string()],
            msg: format!("Invalid content type '{}'", actual_type),
            ctx,
        }
    }

    pub fn too_many_files(field: &str, max_files: usize, actual_count: usize) -> Self {
        let mut ctx = HashMap::new();
        ctx.insert("max_files".to_string(), serde_json::json!(max_files));
        ctx.insert("actual_count".to_string(), serde_json::json!(actual_count));
        ValidationError {
            error_type: "file_too_many".to_string(),
            loc: vec!["body".to_string(), field.to_string()],
            msg: "Too many files uploaded".to_string(),
            ctx,
        }
    }

    pub fn type_coercion_error(field: &str, expected_type: &str, error_msg: &str) -> Self {
        let mut ctx = HashMap::new();
        ctx.insert(
            "expected_type".to_string(),
            serde_json::json!(expected_type),
        );
        ctx.insert("error".to_string(), serde_json::json!(error_msg));
        ValidationError {
            error_type: "type_error".to_string(),
            loc: vec!["body".to_string(), field.to_string()],
            msg: format!("Invalid value for field '{}': {}", field, error_msg),
            ctx,
        }
    }

    /// Convert the validation error into a JSON object shaped for HTTP 422 responses.
    ///
    /// The resulting JSON contains the keys `"type"`, `"loc"`, `"msg"`, and `"ctx"` describing the error.
    ///
    /// # Returns
    ///
    /// A `serde_json::Value` object with the error fields.
    ///
    /// # Examples
    ///
    /// ```
    /// let err = ValidationError::file_too_large("avatar", 1024, 2048);
    /// let json = err.to_json();
    /// assert_eq!(json["type"], "file_too_large");
    /// assert_eq!(json["loc"][0], "body");
    /// assert!(json["ctx"].is_object());
    /// ```
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "type": self.error_type,
            "loc": self.loc,
            "msg": self.msg,
            "ctx": self.ctx
        })
    }
}

/// A form field value. Most form keys appear once (Single — zero extra alloc).
/// Only when a key appears multiple times do we promote to Multi (one Vec alloc
/// at the moment of collision).
#[derive(Debug)]
pub enum FormValue {
    Single(CoercedValue),
    Multi(Vec<CoercedValue>),
}

impl FormValue {
    /// Appends a coerced form value to this `FormValue`, promoting a `Single` into a `Multi` when a second value is inserted.
    ///
    /// # Examples
    ///
    /// ```
    /// // Constructing examples assumes a `CoercedValue` variant `String` exists.
    /// let mut fv = FormValue::Single(CoercedValue::String("first".into()));
    /// fv.append(CoercedValue::String("second".into()));
    /// match fv {
    ///     FormValue::Multi(vals) => {
    ///         assert_eq!(vals.len(), 2);
    ///     }
    ///     _ => panic!("expected Multi after append"),
    /// }
    /// ```
    #[inline]
    fn append(&mut self, value: CoercedValue) {
        match self {
            FormValue::Single(_) => {
                // Take the existing Single, build a 2-element Vec.
                let prev = std::mem::replace(self, FormValue::Multi(Vec::with_capacity(2)));
                if let (FormValue::Single(prev_val), FormValue::Multi(v)) = (prev, &mut *self) {
                    v.push(prev_val);
                    v.push(value);
                }
            }
            FormValue::Multi(v) => v.push(value),
        }
    }
}

/// Result of form parsing
pub struct FormParseResult {
    pub form_map: HashMap<String, FormValue>,
    pub files_map: HashMap<String, Vec<FileInfo>>,
}

/// Parse URL-encoded form data into a map of form fields.
///
/// Fields are coerced according to `type_hints` (a map from field name to numeric type hint).
/// A key that appears once is stored as `FormValue::Single(coerced_value)`; repeated keys are
/// accumulated and promoted to `FormValue::Multi(vec_of_coerced_values)` without allocating a
/// `Vec` for the single-occurrence case.
///
/// Errors:
/// - Returns a `ValidationError` with `error_type = "parse_error"` if the body cannot be parsed.
/// - Returns a `ValidationError` produced by `ValidationError::type_coercion_error` if coercion
///   of any field value fails.
///
/// # Examples
///
/// ```
/// use std::collections::HashMap;
/// let result = parse_urlencoded(b"name=alice&age=30", &HashMap::new(), 8192).unwrap();
/// assert!(matches!(result.get("name").unwrap(), FormValue::Single(_)));
/// assert!(matches!(result.get("age").unwrap(), FormValue::Single(_)));
/// ```
pub fn parse_urlencoded(
    body: &[u8],
    type_hints: &HashMap<String, u8>,
    max_param_length: usize,
) -> Result<HashMap<String, FormValue>, ValidationError> {
    let mut result: HashMap<String, FormValue> = HashMap::new();

    let parsed: Vec<(String, String)> =
        serde_urlencoded::from_bytes(body).map_err(|e| ValidationError {
            error_type: "parse_error".to_string(),
            loc: vec!["body".to_string()],
            msg: format!("Failed to parse form data: {}", e),
            ctx: HashMap::new(),
        })?;

    for (key, value) in parsed {
        let type_hint = type_hints.get(&key).copied().unwrap_or(TYPE_STRING);
        let coerced =
            coerce_param_with_limit(&value, type_hint, max_param_length).map_err(|e| {
                ValidationError::type_coercion_error(
                    &key,
                    type_hint_name(type_hint),
                    &e.to_string(),
                )
            })?;

        match result.entry(key) {
            std::collections::hash_map::Entry::Vacant(e) => {
                e.insert(FormValue::Single(coerced));
            }
            std::collections::hash_map::Entry::Occupied(mut e) => {
                e.get_mut().append(coerced);
            }
        }
    }

    Ok(result)
}

/// Parse a multipart/form-data payload into form fields and uploaded files, spooling large file parts to disk and enforcing per-field constraints.
///
/// This returns a FormParseResult containing:
/// - `form_map`: non-file fields keyed by field name, preserving repeated keys as `FormValue::Multi`.
/// - `files_map`: uploaded files keyed by field name, each as `FileInfo` with filename, content storage, content type, and size.
///
/// Errors are returned as `ValidationError` for parse/read errors, size/type/count violations, or coercion failures.
///
/// # Examples
///
/// ```no_run
/// use std::collections::HashMap;
/// use actix_multipart::Multipart;
/// use tokio_test::block_on;
///
/// // `multipart` would normally come from an Actix request (e.g., HttpRequest payload).
/// let multipart: Multipart = /* obtain Multipart from request */ unimplemented!();
/// let type_hints: HashMap<String, u8> = HashMap::new();
/// let file_constraints: HashMap<String, crate::FileFieldConstraints> = HashMap::new();
///
/// // Run in an async context
/// block_on(async {
///     let result = crate::parse_multipart(
///         multipart,
///         &type_hints,
///         &file_constraints,
///         /* max_upload_size */ 10 * 1024 * 1024,
///         /* memory_limit */ 1024 * 1024,
///         /* max_parts */ 1000,
///         /* max_param_length */ 8192,
///     ).await;
///
///     match result {
///         Ok(parsed) => {
///             // inspect parsed.form_map and parsed.files_map
///         }
///         Err(e) => {
///             // handle validation error
///         }
///     }
/// });
/// ```
pub async fn parse_multipart(
    mut payload: Multipart,
    type_hints: &HashMap<String, u8>,
    file_constraints: &HashMap<String, FileFieldConstraints>,
    max_upload_size: usize,
    memory_limit: usize,
    max_parts: usize,
    max_param_length: usize,
) -> Result<FormParseResult, ValidationError> {
    let mut form_map: HashMap<String, FormValue> = HashMap::new();
    let mut files_map: HashMap<String, Vec<FileInfo>> = HashMap::new();
    let mut part_count = 0;

    while let Some(item) = payload.next().await {
        // Security: Check part count BEFORE expensive field parsing
        part_count += 1;
        if part_count > max_parts {
            return Err(ValidationError {
                error_type: "too_many_parts".to_string(),
                loc: vec!["body".to_string()],
                msg: format!("Too many multipart parts (max {})", max_parts),
                ctx: HashMap::new(),
            });
        }

        let mut field = item.map_err(|e| ValidationError {
            error_type: "multipart_error".to_string(),
            loc: vec!["body".to_string()],
            msg: format!("Failed to read multipart field: {}", e),
            ctx: HashMap::new(),
        })?;

        // Get content disposition - skip if not present
        let Some(content_disposition) = field.content_disposition() else {
            continue;
        };

        let field_name = content_disposition
            .get_name()
            .unwrap_or("unknown")
            .to_string();

        // Check if it's a file upload (has filename)
        if let Some(filename) = content_disposition.get_filename() {
            let filename = filename.to_string();
            let content_type = field
                .content_type()
                .map(|m| m.to_string())
                .unwrap_or_else(|| "application/octet-stream".to_string());

            // Read file content with size limit and disk spooling
            let file_info = read_file_content(
                &mut field,
                &field_name,
                &filename,
                &content_type,
                max_upload_size,
                memory_limit,
            )
            .await?;

            // Validate file if constraints exist
            if let Some(constraints) = file_constraints.get(&field_name) {
                validate_file(&file_info, &field_name, constraints)?;
            }

            // Add to files_map
            files_map
                .entry(field_name.clone())
                .or_default()
                .push(file_info);

            // Validate max_files constraint
            if let Some(constraints) = file_constraints.get(&field_name) {
                if let Some(max_files) = constraints.max_files {
                    let count = files_map.get(&field_name).map(|v| v.len()).unwrap_or(0);
                    if count > max_files {
                        return Err(ValidationError::too_many_files(
                            &field_name,
                            max_files,
                            count,
                        ));
                    }
                }
            }
        } else {
            // Regular form field
            let type_hint = type_hints.get(&field_name).copied().unwrap_or(TYPE_STRING);
            let mut value_bytes = Vec::new();
            while let Some(chunk) = field.next().await {
                let data = chunk.map_err(|e| ValidationError {
                    error_type: "read_error".to_string(),
                    loc: vec!["body".to_string(), field_name.clone()],
                    msg: format!("Failed to read field data: {}", e),
                    ctx: HashMap::new(),
                })?;
                // Security: enforce the length limit incrementally so an oversized
                // field is rejected before the entire body is buffered into memory.
                if value_bytes.len() + data.len() > max_param_length {
                    return Err(ValidationError::type_coercion_error(
                        &field_name,
                        type_hint_name(type_hint),
                        &CoerceError::TooLong {
                            len: value_bytes.len() + data.len(),
                            max: max_param_length,
                        }
                        .to_string(),
                    ));
                }
                value_bytes.extend_from_slice(&data);
            }

            let value = String::from_utf8_lossy(&value_bytes).to_string();

            // Type coercion
            let coerced =
                coerce_param_with_limit(&value, type_hint, max_param_length).map_err(|e| {
                    ValidationError::type_coercion_error(
                        &field_name,
                        type_hint_name(type_hint),
                        &e.to_string(),
                    )
                })?;

            // Preserve duplicate keys (e.g. multi-select) as FormValue::Multi.
            // First occurrence is Single — zero extra alloc for the common case.
            match form_map.entry(field_name) {
                std::collections::hash_map::Entry::Vacant(e) => {
                    e.insert(FormValue::Single(coerced));
                }
                std::collections::hash_map::Entry::Occupied(mut e) => {
                    e.get_mut().append(coerced);
                }
            }
        }
    }

    Ok(FormParseResult {
        form_map,
        files_map,
    })
}

/// Read file content with disk spooling for large files
async fn read_file_content(
    field: &mut actix_multipart::Field,
    field_name: &str,
    filename: &str,
    content_type: &str,
    max_size: usize,
    memory_limit: usize,
) -> Result<FileInfo, ValidationError> {
    let mut size: usize = 0;
    let mut buffer = Vec::new();
    let mut temp_file: Option<NamedTempFile> = None;

    while let Some(chunk) = field.next().await {
        let data = chunk.map_err(|e| ValidationError {
            error_type: "read_error".to_string(),
            loc: vec!["body".to_string()],
            msg: format!("Failed to read file data: {}", e),
            ctx: HashMap::new(),
        })?;

        size += data.len();

        // Check max size limit
        if size > max_size {
            return Err(ValidationError::file_too_large(field_name, max_size, size));
        }

        // Decide whether to keep in memory or spool to disk
        if temp_file.is_none() && size <= memory_limit {
            // Keep in memory
            buffer.extend_from_slice(&data);
        } else {
            // Spool to disk
            if temp_file.is_none() {
                // Create temp file and write existing buffer
                let mut tf = NamedTempFile::new().map_err(|e| ValidationError {
                    error_type: "io_error".to_string(),
                    loc: vec!["body".to_string()],
                    msg: format!("Failed to create temp file: {}", e),
                    ctx: HashMap::new(),
                })?;
                tf.write_all(&buffer).map_err(|e| ValidationError {
                    error_type: "io_error".to_string(),
                    loc: vec!["body".to_string()],
                    msg: format!("Failed to write to temp file: {}", e),
                    ctx: HashMap::new(),
                })?;
                buffer.clear();
                temp_file = Some(tf);
            }

            // Write chunk to temp file
            if let Some(ref mut tf) = temp_file {
                tf.write_all(&data).map_err(|e| ValidationError {
                    error_type: "io_error".to_string(),
                    loc: vec!["body".to_string()],
                    msg: format!("Failed to write to temp file: {}", e),
                    ctx: HashMap::new(),
                })?;
            }
        }
    }

    let content = if let Some(tf) = temp_file {
        FileContent::Disk(tf)
    } else {
        FileContent::Memory(buffer)
    };

    Ok(FileInfo {
        filename: filename.to_string(),
        content,
        content_type: content_type.to_string(),
        size,
    })
}

/// Validate a file against constraints
fn validate_file(
    file: &FileInfo,
    field_name: &str,
    constraints: &FileFieldConstraints,
) -> Result<(), ValidationError> {
    // Check max_size
    if let Some(max_size) = constraints.max_size {
        if file.size > max_size {
            return Err(ValidationError::file_too_large(
                field_name, max_size, file.size,
            ));
        }
    }

    // Check min_size
    if let Some(min_size) = constraints.min_size {
        if file.size < min_size {
            return Err(ValidationError::file_too_small(
                field_name, min_size, file.size,
            ));
        }
    }

    // Check allowed_types with wildcard matching
    if let Some(ref allowed_types) = constraints.allowed_types {
        if !is_content_type_allowed(&file.content_type, allowed_types) {
            return Err(ValidationError::invalid_content_type(
                field_name,
                allowed_types,
                &file.content_type,
            ));
        }
    }

    Ok(())
}

/// Check if content type matches any allowed pattern (supports wildcards like "image/*")
fn is_content_type_allowed(content_type: &str, allowed_types: &[String]) -> bool {
    for pattern in allowed_types {
        if pattern == "*" || pattern == "*/*" {
            return true;
        }

        if pattern.ends_with("/*") {
            // Wildcard pattern like "image/*"
            let prefix = &pattern[..pattern.len() - 1]; // "image/"
            if content_type.starts_with(prefix) {
                return true;
            }
        } else if pattern == content_type {
            // Exact match
            return true;
        }
    }
    false
}

/// Get human-readable type name from type hint
fn type_hint_name(type_hint: u8) -> &'static str {
    use crate::type_coercion::*;
    match type_hint {
        TYPE_INT => "int",
        TYPE_FLOAT => "float",
        TYPE_BOOL => "bool",
        TYPE_STRING => "str",
        TYPE_UUID => "UUID",
        TYPE_DATETIME => "datetime",
        TYPE_DECIMAL => "Decimal",
        TYPE_DATE => "date",
        TYPE_TIME => "time",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use actix_web::web::Bytes;

    #[test]
    fn test_is_content_type_allowed_exact() {
        let allowed = vec!["image/png".to_string(), "image/jpeg".to_string()];
        assert!(is_content_type_allowed("image/png", &allowed));
        assert!(is_content_type_allowed("image/jpeg", &allowed));
        assert!(!is_content_type_allowed("image/gif", &allowed));
        assert!(!is_content_type_allowed("application/pdf", &allowed));
    }

    #[test]
    fn test_is_content_type_allowed_wildcard() {
        let allowed = vec!["image/*".to_string()];
        assert!(is_content_type_allowed("image/png", &allowed));
        assert!(is_content_type_allowed("image/jpeg", &allowed));
        assert!(is_content_type_allowed("image/gif", &allowed));
        assert!(!is_content_type_allowed("application/pdf", &allowed));
    }

    #[test]
    fn test_is_content_type_allowed_all() {
        let allowed = vec!["*/*".to_string()];
        assert!(is_content_type_allowed("image/png", &allowed));
        assert!(is_content_type_allowed("application/pdf", &allowed));
        assert!(is_content_type_allowed("text/plain", &allowed));
    }

    #[test]
    fn test_parse_urlencoded() {
        let body = Bytes::from("name=John&age=30&active=true");
        let mut type_hints = HashMap::new();
        type_hints.insert("name".to_string(), TYPE_STRING);
        type_hints.insert("age".to_string(), crate::type_coercion::TYPE_INT);
        type_hints.insert("active".to_string(), crate::type_coercion::TYPE_BOOL);

        let result = parse_urlencoded(
            &body,
            &type_hints,
            crate::type_coercion::DEFAULT_MAX_PARAM_LENGTH,
        )
        .unwrap();

        assert!(matches!(
            result.get("name"),
            Some(FormValue::Single(CoercedValue::String(s))) if s == "John"
        ));
        assert!(matches!(
            result.get("age"),
            Some(FormValue::Single(CoercedValue::Int(30)))
        ));
        assert!(matches!(
            result.get("active"),
            Some(FormValue::Single(CoercedValue::Bool(true)))
        ));
    }

    /// Ensures repeated URL-encoded form keys are accumulated as `FormValue::Multi` and single-occurrence keys remain `FormValue::Single`.
    ///
    /// # Examples
    ///
    /// ```
    /// // Repeated keys should accumulate into FormValue::Multi
    /// let body = Bytes::from("tag=a&tag=b&tag=c&name=John");
    /// let type_hints = HashMap::new();
    ///
    /// let result = parse_urlencoded(&body, &type_hints, 8192).unwrap();
    ///
    /// match result.get("tag") {
    ///     Some(FormValue::Multi(v)) => {
    ///         assert_eq!(v.len(), 3);
    ///         assert!(matches!(&v[0], CoercedValue::String(s) if s == "a"));
    ///         assert!(matches!(&v[1], CoercedValue::String(s) if s == "b"));
    ///         assert!(matches!(&v[2], CoercedValue::String(s) if s == "c"));
    ///     }
    ///     other => panic!("expected Multi, got {:?}", other),
    /// }
    ///
    /// // Single-occurrence key stays as Single (no Vec alloc)
    /// assert!(matches!(
    ///     result.get("name"),
    ///     Some(FormValue::Single(CoercedValue::String(s))) if s == "John"
    /// ));
    /// ```
    #[test]
    fn test_parse_urlencoded_repeated_key() {
        // Repeated keys should accumulate into FormValue::Multi
        let body = Bytes::from("tag=a&tag=b&tag=c&name=John");
        let type_hints = HashMap::new();

        let result = parse_urlencoded(
            &body,
            &type_hints,
            crate::type_coercion::DEFAULT_MAX_PARAM_LENGTH,
        )
        .unwrap();

        match result.get("tag") {
            Some(FormValue::Multi(v)) => {
                assert_eq!(v.len(), 3);
                assert!(matches!(&v[0], CoercedValue::String(s) if s == "a"));
                assert!(matches!(&v[1], CoercedValue::String(s) if s == "b"));
                assert!(matches!(&v[2], CoercedValue::String(s) if s == "c"));
            }
            other => panic!("expected Multi, got {:?}", other),
        }

        // Single-occurrence key stays as Single (no Vec alloc)
        assert!(matches!(
            result.get("name"),
            Some(FormValue::Single(CoercedValue::String(s))) if s == "John"
        ));
    }

    #[test]
    fn test_validation_error_json() {
        let err = ValidationError::file_too_large("avatar", 1024, 2048);
        let json = err.to_json();

        assert_eq!(json["type"], "file_too_large");
        assert_eq!(json["loc"], serde_json::json!(["body", "avatar"]));
        assert_eq!(json["ctx"]["max_size"], 1024);
        assert_eq!(json["ctx"]["actual_size"], 2048);
    }

    /// A stream of pre-built byte chunks that records how many chunks were
    /// actually polled, and applies backpressure by returning `Pending` after
    /// each chunk.
    ///
    /// The backpressure is essential: actix-multipart's internal `PayloadBuffer`
    /// greedily drains the underlying stream until it sees `Pending` (or EOF).
    /// An always-ready stream would therefore be fully buffered up-front, hiding
    /// whether *our* loop aborted early. Yielding one chunk then `Pending` (with
    /// an immediate self-wake so the task is re-polled) makes the parser pull
    /// chunks strictly on demand, so the counter reflects what our loop consumed.
    struct CountingStream {
        chunks: std::collections::VecDeque<Bytes>,
        consumed: std::sync::Arc<std::sync::atomic::AtomicUsize>,
        yield_pending: bool,
    }

    impl futures_util::Stream for CountingStream {
        type Item = Result<Bytes, actix_web::error::PayloadError>;

        fn poll_next(
            self: std::pin::Pin<&mut Self>,
            cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Self::Item>> {
            let this = self.get_mut();
            if this.yield_pending {
                // Force actix-multipart to stop buffering ahead after one chunk.
                this.yield_pending = false;
                cx.waker().wake_by_ref();
                return std::task::Poll::Pending;
            }
            match this.chunks.pop_front() {
                Some(chunk) => {
                    this.consumed
                        .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    this.yield_pending = true;
                    std::task::Poll::Ready(Some(Ok(chunk)))
                }
                None => std::task::Poll::Ready(None),
            }
        }
    }

    /// The multipart text-field reader must enforce `max_param_length`
    /// *incrementally* — an oversized field is rejected before its entire body
    /// is buffered into memory. We feed the field as many small chunks and
    /// assert the parser stops consuming the stream long before draining all of
    /// them. A post-buffer implementation would drain every chunk first, so this
    /// test fails for that (vulnerable) behavior.
    #[test]
    fn test_multipart_rejects_oversized_field_before_buffering() {
        use actix_web::http::header::{HeaderMap, HeaderValue, CONTENT_TYPE};
        use std::collections::VecDeque;
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

        let boundary = "TESTBOUNDARY";
        let chunk_size = 1000usize;
        let n_data_chunks = 200usize;
        let max_param_length = 1000usize;

        let header = format!(
            "--{b}\r\nContent-Disposition: form-data; name=\"value\"\r\n\r\n",
            b = boundary
        );
        let closing = format!("\r\n--{b}--\r\n", b = boundary);

        let mut chunks: VecDeque<Bytes> = VecDeque::new();
        chunks.push_back(Bytes::from(header));
        for _ in 0..n_data_chunks {
            chunks.push_back(Bytes::from(vec![b'a'; chunk_size]));
        }
        chunks.push_back(Bytes::from(closing));
        let total_chunks = chunks.len();

        let consumed = Arc::new(AtomicUsize::new(0));
        let stream = CountingStream {
            chunks,
            consumed: Arc::clone(&consumed),
            yield_pending: false,
        };

        let mut headers = HeaderMap::new();
        headers.insert(
            CONTENT_TYPE,
            HeaderValue::from_static("multipart/form-data; boundary=TESTBOUNDARY"),
        );

        let multipart = Multipart::new(&headers, stream);
        let type_hints: HashMap<String, u8> = HashMap::new();
        let file_constraints: HashMap<String, FileFieldConstraints> = HashMap::new();

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(parse_multipart(
            multipart,
            &type_hints,
            &file_constraints,
            10 * 1024 * 1024,
            DEFAULT_MEMORY_LIMIT,
            DEFAULT_MAX_PARTS,
            max_param_length,
        ));

        // The oversized field is rejected as "too long".
        let err = match result {
            Ok(_) => panic!("oversized field should be rejected"),
            Err(e) => e,
        };
        assert!(
            err.msg.contains("Parameter too long"),
            "unexpected error message: {}",
            err.msg
        );

        // Crucially: it aborted *before* draining the whole field — far fewer
        // chunks consumed than were provided.
        let consumed_chunks = consumed.load(Ordering::SeqCst);
        assert!(
            consumed_chunks < total_chunks,
            "parser drained {consumed_chunks} of {total_chunks} chunks — it buffered the whole field instead of aborting early"
        );
    }
}
