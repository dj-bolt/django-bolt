//! Form parsing module for URL-encoded and multipart form data
//!
//! Provides Rust-native form parsing with type coercion and file validation,
//! eliminating Python GIL overhead for form-heavy endpoints.

use actix_multipart::Multipart;
use actix_web::error::PayloadError;
use actix_web::web::Bytes;
use futures_util::{Stream, StreamExt};
use std::borrow::Cow;
use std::cell::Cell;
use std::collections::HashMap;
use std::io::Write;
use std::pin::Pin;
use std::rc::Rc;
use std::task::{Context, Poll};
use tempfile::NamedTempFile;

use crate::router::{
    decode_latin1_query_component, decode_query_component, percent_decoded_len, raw_query_pairs,
};
use crate::type_coercion::{CoerceError, CoercedValue, TYPE_STRING, coerce_checked_value};

/// Memory limit for in-memory file storage (1MB default)
pub const DEFAULT_MEMORY_LIMIT: usize = 1024 * 1024;

/// Maximum number of multipart parts allowed
pub const DEFAULT_MAX_PARTS: usize = 100;

/// Maximum number of fields in a URL-encoded form body. Django has the same
/// default for `DATA_UPLOAD_MAX_NUMBER_FIELDS`.
pub const DEFAULT_MAX_FIELDS: usize = 1000;

/// `ValidationError::error_type` value used when the aggregate payload exceeds
/// the configured limit. Callers (handler/test dispatch) match on this to map
/// the error to HTTP 413 instead of the default 422. Defined once here so the
/// producer and consumers can't drift on the string.
pub const ERROR_TYPE_PAYLOAD_TOO_LARGE: &str = "payload_too_large";

/// Build the aggregate-payload-too-large validation error (mapped to HTTP 413).
fn payload_too_large_error(max_total_size: usize) -> ValidationError {
    ValidationError {
        error_type: ERROR_TYPE_PAYLOAD_TOO_LARGE.to_string(),
        loc: vec!["body".to_string()],
        msg: format!("Payload exceeds maximum size of {} bytes", max_total_size),
        ctx: HashMap::new(),
    }
}

/// A request body stream that fails when its bytes pass a limit.
///
/// actix-multipart buffers a part header, and the text before the first
/// boundary, until they end. It has no limit for them. Thus a chunked body
/// with no Content-Length and a header that never ends filled the memory.
/// The aggregate check of `parse_multipart` counts field bytes only, so it
/// did not stop such a body. This stream counts each byte of the body.
pub struct LimitedPayload<S> {
    inner: S,
    remaining: usize,
    exceeded: Rc<Cell<bool>>,
}

impl<S> LimitedPayload<S> {
    /// Wrap `inner`. The returned flag is true after the body passes `limit`.
    pub fn new(inner: S, limit: usize) -> (Self, Rc<Cell<bool>>) {
        let exceeded = Rc::new(Cell::new(false));
        let stream = Self {
            inner,
            remaining: limit,
            exceeded: Rc::clone(&exceeded),
        };
        (stream, exceeded)
    }
}

impl<S> Stream for LimitedPayload<S>
where
    S: Stream<Item = Result<Bytes, PayloadError>> + Unpin,
{
    type Item = Result<Bytes, PayloadError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        match Pin::new(&mut this.inner).poll_next(cx) {
            Poll::Ready(Some(Ok(chunk))) => {
                if chunk.len() > this.remaining {
                    this.exceeded.set(true);
                    return Poll::Ready(Some(Err(PayloadError::Overflow)));
                }
                this.remaining -= chunk.len();
                Poll::Ready(Some(Ok(chunk)))
            }
            other => other,
        }
    }
}

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
    /// ```ignore
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
    /// ```ignore
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
/// The body decodes as Django `request.POST` decodes it. A UTF-8 body decodes as a query
/// string does. Django reads a body that is not UTF-8 as Latin-1, as a whole (`QueryDict`).
/// The UTF-8 check runs once for the body, so a key or value with no escape is not copied.
///
/// Errors:
/// - Returns a `ValidationError` with `error_type = "too_many_fields"` when the body has more
///   than `max_fields` fields. The parser stops at that field, so the rest is not decoded.
/// - Returns a `ValidationError` produced by `ValidationError::type_coercion_error` if coercion
///   of any field value fails, or if a value has more than `max_param_length` bytes after
///   its percent-decoding.
///
/// # Examples
///
/// ```ignore
/// use std::collections::HashMap;
/// let result = parse_urlencoded(b"name=alice&age=30", &HashMap::new(), 8192, 1000).unwrap();
/// assert!(matches!(result.get("name").unwrap(), FormValue::Single(_)));
/// assert!(matches!(result.get("age").unwrap(), FormValue::Single(_)));
/// ```
pub fn parse_urlencoded(
    body: &[u8],
    type_hints: &HashMap<String, u8>,
    max_param_length: usize,
    max_fields: usize,
) -> Result<HashMap<String, FormValue>, ValidationError> {
    let limits = (max_param_length, max_fields);
    match std::str::from_utf8(body) {
        Ok(text) => collect_urlencoded(
            raw_query_pairs(text).map(|(key, value)| {
                (
                    decode_query_component(key),
                    decode_query_component(value),
                    value.as_bytes(),
                )
            }),
            type_hints,
            limits,
        ),
        Err(_) => collect_urlencoded(
            raw_byte_pairs(body).map(|(key, value)| {
                (
                    Cow::Owned(decode_latin1_query_component(key)),
                    Cow::Owned(decode_latin1_query_component(value)),
                    value,
                )
            }),
            type_hints,
            limits,
        ),
    }
}

/// Split a body that is not UTF-8 into raw (key, value) pairs, as `raw_query_pairs` does.
fn raw_byte_pairs(body: &[u8]) -> impl Iterator<Item = (&[u8], &[u8])> {
    body.split(|&byte| byte == b'&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| match memchr::memchr(b'=', pair) {
            Some(at) => (&pair[..at], &pair[at + 1..]),
            None => (pair, &[][..]),
        })
}

/// Coerce and collect decoded (key, value, raw value) fields of a urlencoded body.
/// `limits` holds `max_param_length` and `max_fields`.
fn collect_urlencoded<'a>(
    fields: impl Iterator<Item = (Cow<'a, str>, Cow<'a, str>, &'a [u8])>,
    type_hints: &HashMap<String, u8>,
    (max_param_length, max_fields): (usize, usize),
) -> Result<HashMap<String, FormValue>, ValidationError> {
    let mut result: HashMap<String, FormValue> = HashMap::new();

    for (index, (key, value, raw_value)) in fields.enumerate() {
        if index >= max_fields {
            return Err(ValidationError {
                error_type: "too_many_fields".to_string(),
                loc: vec!["body".to_string()],
                msg: format!("Too many form fields (max {})", max_fields),
                ctx: HashMap::new(),
            });
        }
        let type_hint = type_hints.get(key.as_ref()).copied().unwrap_or(TYPE_STRING);
        // The text is never shorter than the decoded bytes, so only a long
        // text needs the count of its bytes.
        if value.len() > max_param_length {
            let len = percent_decoded_len(raw_value);
            if len > max_param_length {
                return Err(too_long_field(&key, type_hint, len, max_param_length));
            }
        }
        let coerced = coerce_checked_value(value, type_hint).map_err(|e| {
            ValidationError::type_coercion_error(&key, type_hint_name(type_hint), &e.to_string())
        })?;

        match result.entry(key.into_owned()) {
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
/// ```ignore
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
///         /* max_total_size */ 50 * 1024 * 1024,
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
#[expect(
    clippy::too_many_arguments,
    reason = "each limit is a separate setting, and the hot path passes them without a struct"
)]
pub async fn parse_multipart(
    mut payload: Multipart,
    type_hints: &HashMap<String, u8>,
    file_constraints: &HashMap<String, FileFieldConstraints>,
    max_upload_size: usize,
    memory_limit: usize,
    max_parts: usize,
    max_param_length: usize,
    max_total_size: usize,
) -> Result<FormParseResult, ValidationError> {
    let mut form_map: HashMap<String, FormValue> = HashMap::new();
    let mut files_map: HashMap<String, Vec<FileInfo>> = HashMap::new();
    let mut part_count = 0;
    // Aggregate bytes accepted across all parts (files + form fields). Bounds the
    // total payload against the global limit; per-file `max_upload_size` remains
    // the finer-grained limit. Overshoot is at most one part's worth, since each
    // part is already individually bounded.
    let mut total_bytes: usize = 0;

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

            // Enforce aggregate payload limit across all parts. checked_add so a
            // pathological size can't wrap; overflow is treated as over-limit.
            total_bytes = match total_bytes.checked_add(file_info.size) {
                Some(v) if v <= max_total_size => v,
                _ => return Err(payload_too_large_error(max_total_size)),
            };

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
            if let Some(constraints) = file_constraints.get(&field_name)
                && let Some(max_files) = constraints.max_files
            {
                let count = files_map.get(&field_name).map(|v| v.len()).unwrap_or(0);
                if count > max_files {
                    return Err(ValidationError::too_many_files(
                        &field_name,
                        max_files,
                        count,
                    ));
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
                // This is the only length check of the field: it counts the bytes
                // that the client sent, and the decoded text can be longer.
                if value_bytes.len() + data.len() > max_param_length {
                    return Err(too_long_field(
                        &field_name,
                        type_hint,
                        value_bytes.len() + data.len(),
                        max_param_length,
                    ));
                }
                // Enforce aggregate payload limit incrementally so an unbounded
                // form field can't exhaust memory before the part finishes.
                total_bytes = match total_bytes.checked_add(data.len()) {
                    Some(v) if v <= max_total_size => v,
                    _ => return Err(payload_too_large_error(max_total_size)),
                };
                value_bytes.extend_from_slice(&data);
            }

            // Django decodes a text field with `force_str(errors="replace")`.
            // A UTF-8 value moves into the text without a copy.
            let value = match String::from_utf8(value_bytes) {
                Ok(text) => text,
                Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
            };

            let coerced = coerce_checked_value(Cow::Owned(value), type_hint).map_err(|e| {
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
    if let Some(max_size) = constraints.max_size
        && file.size > max_size
    {
        return Err(ValidationError::file_too_large(
            field_name, max_size, file.size,
        ));
    }

    // Check min_size
    if let Some(min_size) = constraints.min_size
        && file.size < min_size
    {
        return Err(ValidationError::file_too_small(
            field_name, min_size, file.size,
        ));
    }

    // Check allowed_types with wildcard matching
    if let Some(ref allowed_types) = constraints.allowed_types
        && !is_content_type_allowed(&file.content_type, allowed_types)
    {
        return Err(ValidationError::invalid_content_type(
            field_name,
            allowed_types,
            &file.content_type,
        ));
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

/// The error of a form value that has more than `max` bytes.
#[cold]
fn too_long_field(field: &str, type_hint: u8, len: usize, max: usize) -> ValidationError {
    ValidationError::type_coercion_error(
        field,
        type_hint_name(type_hint),
        &CoerceError::TooLong { len, max }.to_string(),
    )
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
    fn a_urlencoded_body_accepts_max_fields_and_rejects_one_more() {
        let type_hints = HashMap::new();
        let parse = |body: &[u8]| parse_urlencoded(body, &type_hints, 8, 2);

        assert_eq!(parse(b"a=1&a=2").unwrap().len(), 1);
        // A repeated key counts once for each field. Empty segments are not fields.
        assert!(parse(b"a=1&&b=2&").is_ok());
        let error = match parse(b"a=1&a=2&a=3") {
            Ok(_) => panic!("a third field must be rejected"),
            Err(error) => error,
        };
        assert_eq!(error.error_type, "too_many_fields");
        assert_eq!(error.msg, "Too many form fields (max 2)");
        // The parser stops at the extra field, before it checks its value.
        assert_eq!(
            parse(b"a=1&b=2&c=too-long-value")
                .err()
                .map(|e| e.error_type),
            Some("too_many_fields".to_string())
        );
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
            DEFAULT_MAX_FIELDS,
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
    /// ```ignore
    /// // Repeated keys should accumulate into FormValue::Multi
    /// let body = Bytes::from("tag=a&tag=b&tag=c&name=John");
    /// let type_hints = HashMap::new();
    ///
    /// let result = parse_urlencoded(&body, &type_hints, 8192, 1000).unwrap();
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
            DEFAULT_MAX_FIELDS,
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

    /// Each key of a urlencoded body with all its values, as text, in key order.
    fn urlencoded_text(body: &[u8]) -> Vec<(String, Vec<String>)> {
        let parsed = parse_urlencoded(body, &HashMap::new(), 8192, DEFAULT_MAX_FIELDS).unwrap();
        let text = |value: &CoercedValue| match value {
            CoercedValue::String(text) => text.clone(),
            other => panic!("an untyped field must stay text, got {other:?}"),
        };
        let mut fields: Vec<(String, Vec<String>)> = parsed
            .into_iter()
            .map(|(key, value)| {
                let values = match &value {
                    FormValue::Single(single) => vec![text(single)],
                    FormValue::Multi(many) => many.iter().map(text).collect(),
                };
                (key, values)
            })
            .collect();
        fields.sort();
        fields
    }

    /// A body and the (key, value) fields that it gives.
    type FormRow<'a> = (&'a [u8], &'a [(&'a str, &'a str)]);

    /// Each row is what Django 5.2 and 6.1 give in `request.POST` for the body.
    #[test]
    fn f7_a_body_that_is_not_utf8_reads_as_django_reads_it() {
        let rows: &[FormRow<'_>] = &[
            (b"name=caf\xe9", &[("name", "café")]),
            (b"caf\xe9=1", &[("café", "1")]),
            // Django reads the whole body as Latin-1, so a UTF-8 field changes too.
            (b"a=\xc3\xa9&b=\xe9", &[("a", "Ã©"), ("b", "é")]),
            // The bytes of an escape still decode as UTF-8.
            (b"a=%C3%A9&b=\xe9", &[("a", "é"), ("b", "é")]),
            // Each run of ASCII decodes on its own.
            (b"a=%C3\xa9", &[("a", "\u{FFFD}©")]),
            (b"a=caf\xe9+x%zz", &[("a", "café x%zz")]),
            (b"v=\xc0\xaf", &[("v", "À¯")]),
            (b"v=\xff\xff", &[("v", "ÿÿ")]),
            // A UTF-8 body keeps its rules.
            (b"name=caf%E9", &[("name", "caf\u{FFFD}")]),
            (b"name=caf%C3%A9", &[("name", "café")]),
            (b"name=caf\xc3\xa9", &[("name", "café")]),
            (b"v=%ED%A0%80", &[("v", "\u{FFFD}\u{FFFD}\u{FFFD}")]),
            (b"\xef\xbb\xbfname=x", &[("\u{FEFF}name", "x")]),
        ];
        for (body, expected) in rows {
            let expected: Vec<(String, Vec<String>)> = expected
                .iter()
                .map(|(key, value)| (key.to_string(), vec![value.to_string()]))
                .collect();
            assert_eq!(urlencoded_text(body), expected, "body {body:?}");
        }
    }

    #[test]
    fn a_typed_field_of_a_body_that_is_not_utf8_converts_from_its_text() {
        let type_hints = HashMap::from([("age".to_string(), crate::type_coercion::TYPE_INT)]);
        let parsed = parse_urlencoded(
            b"age=30&name=caf\xe9",
            &type_hints,
            8192,
            DEFAULT_MAX_FIELDS,
        )
        .unwrap();
        assert!(matches!(
            parsed.get("age"),
            Some(FormValue::Single(CoercedValue::Int(30)))
        ));
        assert!(matches!(
            parsed.get("name"),
            Some(FormValue::Single(CoercedValue::String(text))) if text == "café"
        ));
    }

    /// The text of the field `v` with the value, or the error message, under the limit `max`.
    fn urlencoded_value(value: &[u8], max: usize) -> Result<String, String> {
        let mut body = b"v=".to_vec();
        body.extend_from_slice(value);
        match parse_urlencoded(&body, &HashMap::new(), max, DEFAULT_MAX_FIELDS) {
            Ok(mut parsed) => match parsed.remove("v") {
                Some(FormValue::Single(CoercedValue::String(text))) => Ok(text),
                other => panic!("unexpected value {other:?}"),
            },
            Err(error) => Err(error.ctx["error"].as_str().unwrap().to_string()),
        }
    }

    #[test]
    fn f8_a_urlencoded_value_counts_its_bytes_after_the_percent_decoding() {
        let too_long = |len| CoerceError::TooLong { len, max: 17 }.to_string();
        // Escaped bytes that are not UTF-8. The text has three bytes for each.
        assert_eq!(
            urlencoded_value(&b"%FF".repeat(17), 17),
            Ok("\u{FFFD}".repeat(17))
        );
        assert_eq!(urlencoded_value(&b"%FF".repeat(18), 17), Err(too_long(18)));
        // Raw bytes of a body that is not UTF-8. The text has two bytes for each.
        assert_eq!(urlencoded_value(&[0xFF; 17], 17), Ok("ÿ".repeat(17)));
        assert_eq!(urlencoded_value(&[0xFF; 18], 17), Err(too_long(18)));
        // A UTF-8 character counts its bytes.
        assert_eq!(
            urlencoded_value(&b"%C3%A9".repeat(8), 16),
            Ok("é".repeat(8))
        );
        assert_eq!(
            urlencoded_value(&b"%C3%A9".repeat(9), 17),
            Err(too_long(18))
        );
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
        use actix_web::http::header::{CONTENT_TYPE, HeaderMap, HeaderValue};
        use std::collections::VecDeque;
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

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
            usize::MAX,
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

    /// The limits of one `parse_multipart` call. The default limits are not reached.
    struct Limits {
        max_upload_size: usize,
        memory_limit: usize,
        max_parts: usize,
        max_param_length: usize,
        max_total_size: usize,
    }

    impl Default for Limits {
        fn default() -> Self {
            Limits {
                max_upload_size: 1024,
                memory_limit: DEFAULT_MEMORY_LIMIT,
                max_parts: DEFAULT_MAX_PARTS,
                max_param_length: 1024,
                max_total_size: usize::MAX,
            }
        }
    }

    const BOUNDARY: &str = "LIMITBOUNDARY";

    fn text_part(name: &str, value: &[u8]) -> Vec<u8> {
        let mut part =
            format!("--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n")
                .into_bytes();
        part.extend_from_slice(value);
        part.extend_from_slice(b"\r\n");
        part
    }

    fn file_part(name: &str, content_type: &str, data: &[u8]) -> Vec<u8> {
        let mut part = format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"f.bin\"\r\nContent-Type: {content_type}\r\n\r\n"
        )
        .into_bytes();
        part.extend_from_slice(data);
        part.extend_from_slice(b"\r\n");
        part
    }

    /// Parse `parts` as one multipart body that arrives in one chunk.
    fn parse_parts(
        parts: &[Vec<u8>],
        file_constraints: &HashMap<String, FileFieldConstraints>,
        limits: &Limits,
    ) -> Result<FormParseResult, ValidationError> {
        use actix_web::http::header::{CONTENT_TYPE, HeaderMap, HeaderValue};

        let mut body = parts.concat();
        body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
        let stream = futures_util::stream::iter(vec![Ok::<_, actix_web::error::PayloadError>(
            Bytes::from(body),
        )]);
        let mut headers = HeaderMap::new();
        headers.insert(
            CONTENT_TYPE,
            HeaderValue::from_str(&format!("multipart/form-data; boundary={BOUNDARY}")).unwrap(),
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(parse_multipart(
            Multipart::new(&headers, stream),
            &HashMap::new(),
            file_constraints,
            limits.max_upload_size,
            limits.memory_limit,
            limits.max_parts,
            limits.max_param_length,
            limits.max_total_size,
        ))
    }

    fn error_type(result: Result<FormParseResult, ValidationError>) -> String {
        match result {
            Ok(_) => panic!("the body must be rejected"),
            Err(error) => error.error_type,
        }
    }

    #[test]
    fn multipart_accepts_max_parts_and_rejects_one_more() {
        let limits = Limits {
            max_parts: 2,
            ..Limits::default()
        };
        let two = [text_part("a", b"1"), text_part("b", b"2")];
        let three = [
            text_part("a", b"1"),
            text_part("b", b"2"),
            text_part("c", b"3"),
        ];

        assert_eq!(
            parse_parts(&two, &HashMap::new(), &limits)
                .unwrap()
                .form_map
                .len(),
            2
        );
        assert_eq!(
            error_type(parse_parts(&three, &HashMap::new(), &limits)),
            "too_many_parts"
        );
    }

    #[test]
    fn multipart_text_field_of_max_param_length_is_accepted_and_one_more_byte_is_rejected() {
        let limits = Limits {
            max_param_length: 4,
            ..Limits::default()
        };

        let accepted = parse_parts(&[text_part("value", b"abcd")], &HashMap::new(), &limits);
        assert!(matches!(
            accepted.unwrap().form_map.get("value"),
            Some(FormValue::Single(CoercedValue::String(s))) if s == "abcd"
        ));

        let error = match parse_parts(&[text_part("value", b"abcde")], &HashMap::new(), &limits) {
            Ok(_) => panic!("a value over max_param_length must be rejected"),
            Err(error) => error,
        };
        assert_eq!(error.error_type, "type_error");
        assert_eq!(
            error.ctx["error"],
            serde_json::json!(CoerceError::TooLong { len: 5, max: 4 }.to_string())
        );
    }

    /// The limit counts the bytes that the client sent. The text has three
    /// bytes for each byte that is not UTF-8.
    #[test]
    fn f8_a_multipart_field_at_the_byte_limit_passes() {
        let limits = Limits {
            max_param_length: 17,
            ..Limits::default()
        };
        let parse = |value: &[u8]| parse_parts(&[text_part("v", value)], &HashMap::new(), &limits);

        let accepted = parse(&[0xFF; 17]).unwrap();
        assert!(matches!(
            accepted.form_map.get("v"),
            Some(FormValue::Single(CoercedValue::String(text))) if *text == "\u{FFFD}".repeat(17)
        ));
        let error = match parse(&[0xFF; 18]) {
            Ok(_) => panic!("a value over max_param_length must be rejected"),
            Err(error) => error,
        };
        assert_eq!(
            error.ctx["error"],
            serde_json::json!(CoerceError::TooLong { len: 18, max: 17 }.to_string())
        );

        // A UTF-8 character counts its bytes: 8 x 2 bytes and one more.
        let at_limit = format!("{}a", "é".repeat(8));
        assert!(parse(at_limit.as_bytes()).is_ok());
        let over_limit = format!("{}ab", "é".repeat(8));
        assert!(parse(over_limit.as_bytes()).is_err());
    }

    #[test]
    fn multipart_text_field_stops_at_the_first_chunk_over_max_param_length() {
        use actix_web::http::header::{CONTENT_TYPE, HeaderMap, HeaderValue};
        use std::collections::VecDeque;

        // One byte for each chunk, so the field grows by one byte at a time.
        let mut chunks: VecDeque<Bytes> = VecDeque::new();
        chunks.push_back(Bytes::from(format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"value\"\r\n\r\n"
        )));
        for byte in b"abcdef" {
            chunks.push_back(Bytes::copy_from_slice(&[*byte]));
        }
        chunks.push_back(Bytes::from(format!("\r\n--{BOUNDARY}--\r\n")));
        let stream = CountingStream {
            chunks,
            consumed: Default::default(),
            yield_pending: false,
        };
        let mut headers = HeaderMap::new();
        headers.insert(
            CONTENT_TYPE,
            HeaderValue::from_str(&format!("multipart/form-data; boundary={BOUNDARY}")).unwrap(),
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(parse_multipart(
            Multipart::new(&headers, stream),
            &HashMap::new(),
            &HashMap::new(),
            1024,
            DEFAULT_MEMORY_LIMIT,
            DEFAULT_MAX_PARTS,
            3,
            usize::MAX,
        ));

        let error = match result {
            Ok(_) => panic!("a value over max_param_length must be rejected"),
            Err(error) => error,
        };
        // The fourth byte is the first one over the limit.
        assert_eq!(
            error.ctx["error"],
            serde_json::json!(CoerceError::TooLong { len: 4, max: 3 }.to_string())
        );
    }

    #[test]
    fn multipart_text_fields_count_toward_the_total_size() {
        let limits = Limits {
            max_total_size: 5,
            ..Limits::default()
        };
        let parts = [text_part("a", b"abc"), text_part("b", b"abc")];

        assert_eq!(
            error_type(parse_parts(&parts, &HashMap::new(), &limits)),
            ERROR_TYPE_PAYLOAD_TOO_LARGE
        );
    }

    #[test]
    fn multipart_files_count_toward_the_total_size() {
        let limits = Limits {
            max_total_size: 6,
            ..Limits::default()
        };
        let at_limit = [
            file_part("a", "text/plain", b"abc"),
            file_part("b", "text/plain", b"abc"),
        ];
        let over_limit = [
            file_part("a", "text/plain", b"abc"),
            file_part("b", "text/plain", b"abcd"),
        ];

        assert!(parse_parts(&at_limit, &HashMap::new(), &limits).is_ok());
        assert_eq!(
            error_type(parse_parts(&over_limit, &HashMap::new(), &limits)),
            ERROR_TYPE_PAYLOAD_TOO_LARGE
        );
    }

    #[test]
    fn multipart_file_of_max_upload_size_is_accepted_and_one_more_byte_is_rejected() {
        let limits = Limits {
            max_upload_size: 4,
            ..Limits::default()
        };

        let accepted = parse_parts(
            &[file_part("doc", "text/plain", b"abcd")],
            &HashMap::new(),
            &limits,
        );
        assert_eq!(accepted.unwrap().files_map["doc"][0].size, 4);

        let rejected = parse_parts(
            &[file_part("doc", "text/plain", b"abcde")],
            &HashMap::new(),
            &limits,
        );
        assert_eq!(error_type(rejected), "file_too_large");
    }

    #[test]
    fn multipart_file_of_the_memory_limit_stays_in_memory_and_one_more_byte_goes_to_disk() {
        let limits = Limits {
            memory_limit: 4,
            ..Limits::default()
        };
        let upload = |data: &[u8]| {
            let parts = [file_part("upload", "text/plain", data)];
            let mut parsed = parse_parts(&parts, &HashMap::new(), &limits).unwrap();
            parsed.files_map.remove("upload").unwrap().remove(0)
        };

        let at_limit = upload(b"abcd");
        assert!(matches!(&at_limit.content, FileContent::Memory(data) if data == b"abcd"));

        let over_limit = upload(b"abcde");
        let FileContent::Disk(file) = &over_limit.content else {
            panic!("a file over the memory limit must go to a temporary file");
        };
        assert_eq!(std::fs::read(file.path()).unwrap(), b"abcde");
        assert_eq!(over_limit.size, 5);
    }

    #[test]
    fn multipart_file_of_its_max_size_is_accepted_and_one_more_byte_is_rejected() {
        let constraints = HashMap::from([(
            "doc".to_string(),
            FileFieldConstraints {
                max_size: Some(4),
                ..FileFieldConstraints::default()
            },
        )]);

        let accepted = parse_parts(
            &[file_part("doc", "text/plain", b"abcd")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(accepted.unwrap().files_map["doc"][0].size, 4);

        let rejected = parse_parts(
            &[file_part("doc", "text/plain", b"abcde")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(error_type(rejected), "file_too_large");
    }

    #[test]
    fn multipart_file_of_its_min_size_is_accepted_and_one_less_byte_is_rejected() {
        let constraints = HashMap::from([(
            "doc".to_string(),
            FileFieldConstraints {
                min_size: Some(4),
                ..FileFieldConstraints::default()
            },
        )]);

        let accepted = parse_parts(
            &[file_part("doc", "text/plain", b"abcd")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(accepted.unwrap().files_map["doc"][0].size, 4);

        let rejected = parse_parts(
            &[file_part("doc", "text/plain", b"abc")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(error_type(rejected), "file_too_small");
    }

    #[test]
    fn multipart_field_with_its_max_files_is_accepted_and_one_more_file_is_rejected() {
        let constraints = HashMap::from([(
            "doc".to_string(),
            FileFieldConstraints {
                max_files: Some(2),
                ..FileFieldConstraints::default()
            },
        )]);
        let file = || file_part("doc", "text/plain", b"abc");

        let accepted = parse_parts(&[file(), file()], &constraints, &Limits::default());
        assert_eq!(accepted.unwrap().files_map["doc"].len(), 2);

        let rejected = parse_parts(&[file(), file(), file()], &constraints, &Limits::default());
        assert_eq!(error_type(rejected), "file_too_many");
    }

    #[test]
    fn multipart_file_of_an_allowed_type_is_accepted_and_another_type_is_rejected() {
        let constraints = HashMap::from([(
            "doc".to_string(),
            FileFieldConstraints {
                allowed_types: Some(vec!["image/*".to_string()]),
                ..FileFieldConstraints::default()
            },
        )]);

        let accepted = parse_parts(
            &[file_part("doc", "image/png", b"abc")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(
            accepted.unwrap().files_map["doc"][0].content_type,
            "image/png"
        );

        let rejected = parse_parts(
            &[file_part("doc", "text/plain", b"abc")],
            &constraints,
            &Limits::default(),
        );
        assert_eq!(error_type(rejected), "file_invalid_content_type");
    }

    #[test]
    fn a_coercion_error_names_the_expected_type() {
        use crate::type_coercion::*;

        for (type_hint, name) in [
            (TYPE_INT, "int"),
            (TYPE_FLOAT, "float"),
            (TYPE_BOOL, "bool"),
            (TYPE_STRING, "str"),
            (TYPE_UUID, "UUID"),
            (TYPE_DATETIME, "datetime"),
            (TYPE_DECIMAL, "Decimal"),
            (TYPE_DATE, "date"),
            (TYPE_TIME, "time"),
            (0, "unknown"),
        ] {
            // The value is over max_param_length, so every type rejects it.
            let type_hints = HashMap::from([("f".to_string(), type_hint)]);
            let error = match parse_urlencoded(b"f=xy", &type_hints, 1, DEFAULT_MAX_FIELDS) {
                Ok(_) => panic!("a value over max_param_length must be rejected"),
                Err(error) => error,
            };
            assert_eq!(error.ctx["expected_type"], serde_json::json!(name));
        }
    }

    #[test]
    fn a_limited_payload_stops_a_part_header_that_never_ends() {
        use actix_web::http::header::{CONTENT_TYPE, HeaderMap, HeaderValue};
        use std::collections::VecDeque;
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

        let mut chunks: VecDeque<Bytes> = VecDeque::new();
        chunks.push_back(Bytes::from(format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"value\"\r\nX-Pad: "
        )));
        for _ in 0..200 {
            chunks.push_back(Bytes::from(vec![b'a'; 1000]));
        }
        let total_chunks = chunks.len();
        let consumed = Arc::new(AtomicUsize::new(0));
        let stream = CountingStream {
            chunks,
            consumed: Arc::clone(&consumed),
            yield_pending: false,
        };
        let (limited, exceeded) = LimitedPayload::new(stream, 4096);
        let mut headers = HeaderMap::new();
        headers.insert(
            CONTENT_TYPE,
            HeaderValue::from_str(&format!("multipart/form-data; boundary={BOUNDARY}")).unwrap(),
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(parse_multipart(
            Multipart::new(&headers, limited),
            &HashMap::new(),
            &HashMap::new(),
            1024,
            DEFAULT_MEMORY_LIMIT,
            DEFAULT_MAX_PARTS,
            1024,
            usize::MAX,
        ));

        assert!(result.is_err());
        assert!(exceeded.get());
        // The header and five chunks of 1000 bytes pass 4096 bytes.
        let consumed = consumed.load(Ordering::SeqCst);
        assert!(consumed <= 6, "read {consumed} of {total_chunks} chunks");
    }

    const FORM_PIECES: &[&str] = &["%FF", "%C3", "%C3%A9", "%zz", "%", "+", "=", "é", "a"];

    /// A value of a form body: escapes, valid and not, and any byte but `&`.
    fn form_value_bytes() -> impl proptest::strategy::Strategy<Value = Vec<u8>> {
        use proptest::prelude::*;
        let piece = prop_oneof![
            prop::sample::select(FORM_PIECES).prop_map(|piece| piece.as_bytes().to_vec()),
            any::<u8>()
                .prop_filter("& ends a field", |byte| *byte != b'&')
                .prop_map(|byte| vec![byte]),
        ];
        prop::collection::vec(piece, 0..24).prop_map(|pieces| pieces.concat())
    }

    /// A UTF-8 value of a form body: escapes, valid and not, and any character but `&`.
    fn form_value_text() -> impl proptest::strategy::Strategy<Value = String> {
        use proptest::prelude::*;
        let piece = prop_oneof![
            prop::sample::select(FORM_PIECES).prop_map(str::to_string),
            any::<char>()
                .prop_filter("& ends a field", |character| *character != '&')
                .prop_map(String::from),
        ];
        prop::collection::vec(piece, 0..24).prop_map(|pieces| pieces.concat())
    }

    proptest::proptest! {
        /// A value of `max_param_length` decoded bytes passes, and one byte less
        /// is too long. The error gives the decoded byte count.
        #[test]
        fn a_urlencoded_value_is_checked_on_its_decoded_bytes(value in form_value_bytes()) {
            let len = urlencoding::decode_binary(&value).len();
            proptest::prop_assert!(urlencoded_value(&value, len).is_ok());
            if len > 0 {
                let too_long = CoerceError::TooLong { len, max: len - 1 }.to_string();
                proptest::prop_assert_eq!(urlencoded_value(&value, len - 1), Err(too_long));
            }
        }

        /// A UTF-8 body gives the pairs of the WHATWG parser, as before.
        #[test]
        fn a_utf8_body_parses_as_the_whatwg_parser(value in form_value_text()) {
            let body = format!("k={value}&{value}");
            let body = body.as_bytes();
            let mut expected: HashMap<String, Vec<String>> = HashMap::new();
            for (key, value) in serde_urlencoded::from_bytes::<Vec<(String, String)>>(body).unwrap() {
                expected.entry(key).or_default().push(value);
            }
            let mut expected: Vec<(String, Vec<String>)> = expected.into_iter().collect();
            expected.sort();
            proptest::prop_assert_eq!(urlencoded_text(body), expected);
        }

        /// A multipart text field decodes as UTF-8 with U+FFFD, and the limit
        /// counts the bytes that the client sent.
        #[test]
        fn a_multipart_value_is_checked_on_its_bytes(value in proptest::collection::vec(proptest::prelude::any::<u8>(), 1..40)) {
            proptest::prop_assume!(!value.windows(BOUNDARY.len()).any(|w| w == BOUNDARY.as_bytes()));
            let parse = |max| {
                let limits = Limits { max_param_length: max, ..Limits::default() };
                parse_parts(&[text_part("v", &value)], &HashMap::new(), &limits)
            };
            let parsed = parse(value.len()).unwrap();
            let expected = String::from_utf8_lossy(&value).into_owned();
            proptest::prop_assert!(matches!(
                parsed.form_map.get("v"),
                Some(FormValue::Single(CoercedValue::String(text))) if *text == expected
            ));
            let error = parse(value.len() - 1).err().unwrap();
            let too_long = CoerceError::TooLong { len: value.len(), max: value.len() - 1 }.to_string();
            proptest::prop_assert_eq!(&error.ctx["error"], &serde_json::json!(too_long));
        }
    }

    #[test]
    fn a_limited_payload_passes_a_body_of_the_limit() {
        let chunks = vec![
            Ok::<_, PayloadError>(Bytes::from_static(b"abc")),
            Ok(Bytes::from_static(b"de")),
        ];
        let (limited, exceeded) = LimitedPayload::new(futures_util::stream::iter(chunks), 5);
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let items: Vec<_> = runtime.block_on(limited.collect());
        assert_eq!(items.len(), 2);
        assert!(items.iter().all(Result::is_ok));
        assert!(!exceeded.get());
    }
}
