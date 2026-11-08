/// Route metadata parsing from Python to Rust types
///
/// This module handles parsing Python metadata dicts into strongly-typed
/// Rust enums at registration time, eliminating per-request GIL overhead.
use actix_web::http::header::HeaderValue;
use ahash::{AHashMap, AHashSet};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyList, PyString};
use regex::Regex;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use crate::form_parsing::FileFieldConstraints;
use crate::middleware::auth::{
    ApiKeys, AuthBackend, AuthContext, Claims, JwtKeySource, RefreshingJwks, StaticKey,
    build_jwks_key_source, build_jwt_decoding_key, parse_jwt_algorithm,
};
use crate::permissions::{
    ClaimKey, Guard, GuardDenial, GuardResult, GuardSet, Quantifier, evaluate_guards,
};
use crate::responses::{ERROR_BODY_401, ERROR_BODY_403};
use crate::type_coercion::TypeHints;

/// Request value source for Rust-side argument prebinding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RustArgSource {
    Path,
    Query,
    Header,
    Cookie,
}

/// One argument binding entry used by Rust-side prebinding.
///
/// `key` is the wire name that Rust looks up in its request maps. `arg_name`
/// is an interned Python string created once at registration, so the
/// per-request kwargs insert reuses one object.
#[derive(Debug)]
pub struct RustArgBinding {
    pub source: RustArgSource,
    /// Wire key, for lookups in the request maps.
    pub key: String,
    pub arg_name: Py<PyString>,
    pub positional: bool,
    // (Clone is implemented manually below — Py<T> needs the GIL to clone_ref.)
    /// Required params fall back to the Python injector when missing (which
    /// raises the proper 422). Optional params are skipped or None-injected.
    pub required: bool,
    /// True when the handler signature has no plain default for this optional
    /// param (Optional[T] annotation only) — Rust injects None explicitly.
    /// False → the binding is simply omitted so the function default applies.
    pub inject_none: bool,
}

impl Clone for RustArgBinding {
    fn clone(&self) -> Self {
        // Only exercised at startup (global-CORS metadata rebuild) and on the
        // test-app path — never on the production request hot path.
        Python::attach(|py| Self {
            source: self.source,
            key: self.key.clone(),
            arg_name: self.arg_name.clone_ref(py),
            positional: self.positional,
            required: self.required,
            inject_none: self.inject_none,
        })
    }
}

/// CORS configuration parsed at startup
#[derive(Debug, Clone)]
pub struct CorsConfig {
    pub origins: Vec<String>,
    pub origin_regexes: Vec<String>, // Stored as strings for serialization
    pub compiled_origin_regexes: Vec<Regex>, // Compiled regex patterns for O(1) matching
    pub origin_set: AHashSet<String>, // O(1) lookup for exact origin matches
    pub allow_all_origins: bool,
    pub credentials: bool,
    pub methods: Vec<String>,
    pub headers: Vec<String>,
    pub expose_headers: Vec<String>,
    pub max_age: u32,
    // Pre-computed header strings to avoid per-request allocations
    pub methods_str: String,
    pub headers_str: String,
    pub expose_headers_str: String,
    pub max_age_str: String,
    // Pre-cached HeaderValue for zero-allocation header injection
    pub methods_header: Option<HeaderValue>,
    pub headers_header: Option<HeaderValue>,
    pub expose_headers_header: Option<HeaderValue>,
    pub max_age_header: Option<HeaderValue>,
}

impl Default for CorsConfig {
    fn default() -> Self {
        let methods = vec![
            "GET".to_string(),
            "POST".to_string(),
            "PUT".to_string(),
            "PATCH".to_string(),
            "DELETE".to_string(),
            "OPTIONS".to_string(),
            "QUERY".to_string(),
        ];
        let headers = vec!["Content-Type".to_string(), "Authorization".to_string()];
        let expose_headers = vec![];
        let max_age = 3600;

        let methods_str = methods.join(", ");
        let headers_str = headers.join(", ");
        let expose_headers_str = expose_headers.join(", ");
        let max_age_str = max_age.to_string();

        // Pre-cache HeaderValue for zero-allocation header injection
        let methods_header = HeaderValue::from_str(&methods_str).ok();
        let headers_header = HeaderValue::from_str(&headers_str).ok();
        let expose_headers_header = if expose_headers.is_empty() {
            None
        } else {
            HeaderValue::from_str(&expose_headers_str).ok()
        };
        let max_age_header = HeaderValue::from_str(&max_age_str).ok();

        CorsConfig {
            origins: vec![],
            origin_regexes: vec![],
            compiled_origin_regexes: vec![],
            origin_set: AHashSet::new(),
            allow_all_origins: false,
            credentials: false,
            methods_str,
            headers_str,
            expose_headers_str,
            max_age_str,
            methods,
            headers,
            expose_headers,
            max_age,
            methods_header,
            headers_header,
            expose_headers_header,
            max_age_header,
        }
    }
}

impl CorsConfig {
    /// Create CorsConfig from Django settings (django-cors-headers compatible)
    #[expect(
        clippy::too_many_arguments,
        reason = "one argument for each django-cors-headers setting"
    )]
    pub fn from_django_settings(
        origins: Vec<String>,
        origin_regexes: Vec<String>,
        allow_all_origins: bool,
        allow_credentials: bool,
        allow_methods: Option<Vec<String>>,
        allow_headers: Option<Vec<String>>,
        expose_headers: Option<Vec<String>>,
        max_age: Option<u32>,
    ) -> Self {
        // Compile origin regex patterns at startup
        let compiled_origin_regexes = origin_regexes
            .iter()
            .filter_map(|pattern| {
                Regex::new(pattern).ok().or_else(|| {
                    eprintln!(
                        "[django-bolt] Warning: Invalid CORS origin regex pattern: {}",
                        pattern
                    );
                    None
                })
            })
            .collect();

        let mut config = CorsConfig {
            // Build origin set for O(1) lookups
            origin_set: origins.iter().cloned().collect(),
            origins,
            origin_regexes,
            compiled_origin_regexes,
            allow_all_origins,
            credentials: allow_credentials,
            ..Default::default()
        };

        if let Some(methods) = allow_methods {
            config.methods_str = methods.join(", ");
            config.methods_header = HeaderValue::from_str(&config.methods_str).ok();
            config.methods = methods;
        }

        if let Some(headers) = allow_headers {
            config.headers_str = headers.join(", ");
            config.headers_header = HeaderValue::from_str(&config.headers_str).ok();
            config.headers = headers;
        }

        if let Some(expose) = expose_headers {
            config.expose_headers_str = expose.join(", ");
            config.expose_headers_header = if !expose.is_empty() {
                HeaderValue::from_str(&config.expose_headers_str).ok()
            } else {
                None
            };
            config.expose_headers = expose;
        }

        if let Some(age) = max_age {
            config.max_age_str = age.to_string();
            config.max_age_header = HeaderValue::from_str(&config.max_age_str).ok();
            config.max_age = age;
        }

        config
    }
}

/// Rate limiting configuration parsed at startup
#[derive(Debug, Clone)]
pub struct RateLimitConfig {
    pub rps: u32,
    pub burst: u32,
    pub key: RateLimitKey,
}

/// What a rate limit buckets on. Decided once at registration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RateLimitKey {
    /// The resolved client address. See `middleware::client_ip`.
    Ip,
    /// One request header, stored lowercase for direct lookup.
    Header(String),
    /// The authenticated identity from any backend (`AuthContext::user_id`).
    /// Runs after authentication. No identity falls back to the client address.
    User,
    /// The authenticated API key. Other backends do not count as an identity.
    ApiKey,
}

impl RateLimitKey {
    /// `User` and `ApiKey` need an `AuthContext`, so their check runs after
    /// authentication instead of before it.
    pub const fn needs_identity(&self) -> bool {
        matches!(self, RateLimitKey::User | RateLimitKey::ApiKey)
    }
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        RateLimitConfig {
            rps: 100,
            burst: 200,
            key: RateLimitKey::Ip,
        }
    }
}

/// Compression configuration parsed at startup
#[derive(Debug, Clone)]
pub struct CompressionConfig {
    pub backend: String,     // "gzip", "brotli", "zstd"
    pub minimum_size: usize, // Minimum response size to compress (bytes)
    pub gzip_fallback: bool, // Fall back to gzip if backend not supported
    pub brotli_level: u32,   // 0..=11
    pub brotli_lgwin: u32,   // 10..=24
    pub gzip_level: u32,     // 0..=9
    pub zstd_level: u32,     // 1..=22
}

impl Default for CompressionConfig {
    fn default() -> Self {
        CompressionConfig {
            backend: "brotli".to_string(),
            minimum_size: 500,
            gzip_fallback: true,
            brotli_level: 5,
            brotli_lgwin: 14,
            gzip_level: 6,
            zstd_level: 3,
        }
    }
}

impl CompressionConfig {
    /// Parse a `CompressionConfig.to_rust_config()` dict from Python.
    ///
    /// Required fields (`backend`, `minimum_size`, `gzip_fallback`) raise a
    /// Python `ValueError` if missing or malformed. Optional tuning fields
    /// fall back to defaults when absent, but a *present-but-malformed*
    /// value (wrong type or out-of-range) also raises — silently coercing a
    /// typo'd `brotli_level=99` to the default would make tuning regressions
    /// invisible.
    ///
    /// Takes `&Bound<PyAny>` (not `&Bound<PyDict>`) so the same call shape
    /// works for both `server.rs` (Python dict bound as `Py<PyAny>`) and
    /// `testing.rs` (typed `Bound<PyDict>` — `as_any()` for the same shape).
    pub fn from_python_dict(dict: &pyo3::Bound<'_, pyo3::PyAny>) -> pyo3::PyResult<Self> {
        use pyo3::exceptions::PyValueError;

        let defaults = Self::default();
        let require = |key: &str| -> pyo3::PyResult<pyo3::Bound<'_, pyo3::PyAny>> {
            dict.get_item(key).and_then(|v| {
                if v.is_none() {
                    Err(PyValueError::new_err(format!(
                        "CompressionConfig: required field '{}' is missing",
                        key
                    )))
                } else {
                    Ok(v)
                }
            })
        };
        let backend: String = require("backend")?
            .extract()
            .map_err(|_| PyValueError::new_err("CompressionConfig: 'backend' must be a string"))?;
        let minimum_size: usize = require("minimum_size")?.extract().map_err(|_| {
            PyValueError::new_err("CompressionConfig: 'minimum_size' must be a non-negative int")
        })?;
        let gzip_fallback: bool = require("gzip_fallback")?.extract().map_err(|_| {
            PyValueError::new_err("CompressionConfig: 'gzip_fallback' must be a bool")
        })?;

        // Optional tuning field: missing → fallback to default; present but
        // unparseable / out-of-range → raise so the user notices the typo.
        let parse_u32 = |key: &str, fallback: u32, lo: u32, hi: u32| -> pyo3::PyResult<u32> {
            match dict.get_item(key) {
                Ok(v) if !v.is_none() => {
                    let value = v.extract::<u32>().map_err(|_| {
                        PyValueError::new_err(format!(
                            "CompressionConfig: '{}' must be an int in [{}, {}]",
                            key, lo, hi
                        ))
                    })?;
                    if value < lo || value > hi {
                        return Err(PyValueError::new_err(format!(
                            "CompressionConfig: '{}'={} out of range [{}, {}]",
                            key, value, lo, hi
                        )));
                    }
                    Ok(value)
                }
                _ => Ok(fallback),
            }
        };
        Ok(CompressionConfig {
            backend,
            minimum_size,
            gzip_fallback,
            brotli_level: parse_u32("brotli_level", defaults.brotli_level, 0, 11)?,
            brotli_lgwin: parse_u32("brotli_lgwin", defaults.brotli_lgwin, 10, 24)?,
            gzip_level: parse_u32("gzip_level", defaults.gzip_level, 0, 9)?,
            zstd_level: parse_u32("zstd_level", defaults.zstd_level, 1, 22)?,
        })
    }
}

/// Compact per-route execution plan used in request hot paths.
#[derive(Debug, Clone, Copy, Default)]
pub struct RouteExecutionPlan {
    bits: u16,
}

impl RouteExecutionPlan {
    const NEEDS_BODY: u16 = 1 << 0;
    const NEEDS_QUERY: u16 = 1 << 1;
    const NEEDS_HEADERS: u16 = 1 << 2;
    const NEEDS_COOKIES: u16 = 1 << 3;
    const NEEDS_FORM_PARSING: u16 = 1 << 4;
    const SKIP_CORS: u16 = 1 << 5;
    const SKIP_COMPRESSION: u16 = 1 << 6;
    const HAS_AUTH_OR_GUARDS: u16 = 1 << 7;
    const HAS_RATE_LIMIT: u16 = 1 << 8;
    const CAN_SYNC_DISPATCH: u16 = 1 << 9;
    const IS_ASYNC: u16 = 1 << 10;
    const CAN_LANE_DISPATCH: u16 = 1 << 11;

    #[allow(clippy::too_many_arguments)]
    pub fn from_parts(
        needs_body: bool,
        needs_query: bool,
        needs_headers: bool,
        needs_cookies: bool,
        needs_form_parsing: bool,
        skip_cors: bool,
        skip_compression: bool,
        has_auth_or_guards: bool,
        has_rate_limit: bool,
        can_sync_dispatch: bool,
        is_async: bool,
        can_lane_dispatch: bool,
    ) -> Self {
        let mut bits = 0u16;
        if needs_body {
            bits |= Self::NEEDS_BODY;
        }
        if needs_query {
            bits |= Self::NEEDS_QUERY;
        }
        if needs_headers {
            bits |= Self::NEEDS_HEADERS;
        }
        if needs_cookies {
            bits |= Self::NEEDS_COOKIES;
        }
        if needs_form_parsing {
            bits |= Self::NEEDS_FORM_PARSING;
        }
        if skip_cors {
            bits |= Self::SKIP_CORS;
        }
        if skip_compression {
            bits |= Self::SKIP_COMPRESSION;
        }
        if has_auth_or_guards {
            bits |= Self::HAS_AUTH_OR_GUARDS;
        }
        if has_rate_limit {
            bits |= Self::HAS_RATE_LIMIT;
        }
        if can_sync_dispatch {
            bits |= Self::CAN_SYNC_DISPATCH;
        }
        if is_async {
            bits |= Self::IS_ASYNC;
        }
        if can_lane_dispatch {
            bits |= Self::CAN_LANE_DISPATCH;
        }
        Self { bits }
    }

    #[inline]
    pub const fn needs_body(self) -> bool {
        (self.bits & Self::NEEDS_BODY) != 0
    }

    #[inline]
    pub const fn needs_query(self) -> bool {
        (self.bits & Self::NEEDS_QUERY) != 0
    }

    #[inline]
    pub const fn needs_headers(self) -> bool {
        (self.bits & Self::NEEDS_HEADERS) != 0
    }

    #[inline]
    pub const fn needs_cookies(self) -> bool {
        (self.bits & Self::NEEDS_COOKIES) != 0
    }

    #[inline]
    pub const fn needs_form_parsing(self) -> bool {
        (self.bits & Self::NEEDS_FORM_PARSING) != 0
    }

    #[inline]
    pub const fn skip_cors(self) -> bool {
        (self.bits & Self::SKIP_CORS) != 0
    }

    #[inline]
    pub const fn skip_compression(self) -> bool {
        (self.bits & Self::SKIP_COMPRESSION) != 0
    }

    #[inline]
    pub const fn has_auth_or_guards(self) -> bool {
        (self.bits & Self::HAS_AUTH_OR_GUARDS) != 0
    }

    #[inline]
    pub const fn has_rate_limit(self) -> bool {
        (self.bits & Self::HAS_RATE_LIMIT) != 0
    }

    #[inline]
    pub const fn can_sync_dispatch(self) -> bool {
        (self.bits & Self::CAN_SYNC_DISPATCH) != 0
    }

    #[inline]
    pub const fn can_lane_dispatch(self) -> bool {
        (self.bits & Self::CAN_LANE_DISPATCH) != 0
    }

    #[inline]
    pub const fn is_async(self) -> bool {
        (self.bits & Self::IS_ASYNC) != 0
    }
}

/// Dense metadata table keyed by handler_id.
#[derive(Debug, Clone, Default)]
pub struct RouteMetadataStore {
    by_handler_id: Vec<Option<RouteMetadata>>,
}

impl RouteMetadataStore {
    pub fn from_map(map: AHashMap<usize, RouteMetadata>) -> Self {
        if map.is_empty() {
            return Self::default();
        }

        let max_id = map.keys().copied().max().unwrap_or(0);
        let mut by_handler_id: Vec<Option<RouteMetadata>> = (0..=max_id).map(|_| None).collect();
        for (handler_id, metadata) in map {
            by_handler_id[handler_id] = Some(metadata);
        }
        Self { by_handler_id }
    }

    #[inline]
    pub fn get(&self, handler_id: usize) -> Option<&RouteMetadata> {
        self.by_handler_id
            .get(handler_id)
            .and_then(|metadata| metadata.as_ref())
    }
}

/// Complete route metadata including middleware, auth, and guards
#[derive(Debug, Clone)]
pub struct RouteMetadata {
    pub auth_backends: Vec<AuthBackend>,
    /// Cookie names whose presence subjects unsafe requests to the CSRF
    /// origin check. Precomputed from `auth_backends` at registration so
    /// the hot path never re-scans the backend list.
    pub guards: GuardSet,
    pub skip: HashSet<String>,
    pub cors_config: Option<CorsConfig>,
    pub rate_limit_config: Option<RateLimitConfig>,

    #[allow(dead_code)] // Reserved for future optimization
    pub needs_path_params: bool,
    #[allow(dead_code)] // Reserved for future optimization
    pub is_static_route: bool,

    // Type hints for path and for query parameters (enables Rust-side type coercion).
    // Each maps a wire key to a type hint constant (see type_coercion.rs).
    // A key of one source does not apply to the other.
    pub path_types: TypeHints,
    pub query_types: TypeHints,
    // Type hints for header and cookie parameters, keyed by wire name.
    // A header key is lowercase with hyphens. A cookie key is the cookie name.
    // Only non-string types are present.
    pub header_types: TypeHints,
    pub cookie_types: TypeHints,

    // Form-related metadata for Rust-side form parsing
    pub form_type_hints: HashMap<String, u8>,
    // Field names whose annotated type is a sequence (list/set/tuple/frozenset).
    // Rust always emits a Python list for these keys so the Python extractor never
    // has to wrap a single occurrence — eliminates a per-request loop.
    pub form_seq_fields: HashSet<String>,
    // Query keys whose annotated type is a sequence. A repeated key gives each of
    // its values as a Python list; the plain query map keeps the last value only.
    pub query_seq_fields: AHashSet<String>,
    pub file_constraints: HashMap<String, FileFieldConstraints>,
    pub max_upload_size: usize,
    pub memory_spool_threshold: usize,
    pub rust_arg_bindings: Option<Vec<RustArgBinding>>,
    /// True when the bindings are the only reader of the request maps: no
    /// request parameter, dependency or middleware. Rust then binds the
    /// arguments from its own maps and builds no Python source dicts.
    pub prebind_only: bool,
    pub plan: RouteExecutionPlan,
    /// Route's default success status code. Used by the bare-bytes response
    /// fast path: sync executors may return just the encoded JSON body and
    /// Rust rebuilds the (status, JSON meta) envelope from this value.
    pub default_status_code: u16,
    /// WebSocket routes with a `revoked_token_handler`: an async Python
    /// callable that takes the auth context and returns True for a revoked
    /// token. The handshake awaits it before the upgrade.
    pub websocket_revocation_check: Option<Arc<Py<PyAny>>>,
}

/// Read a byte size of the route metadata, or the default when the key is missing.
/// A value that is not an int of 0 or more is a TypeError.
fn optional_size(py_meta: &Bound<'_, PyDict>, key: &str, default: usize) -> PyResult<usize> {
    let Some(value) = py_meta.get_item(key)? else {
        return Ok(default);
    };
    if !value.is_instance_of::<PyBool>()
        && let Ok(size) = value.extract::<usize>()
    {
        return Ok(size);
    }
    Err(PyTypeError::new_err(format!(
        "Route metadata '{}' must be an int of 0 or more, got {}",
        key,
        value.get_type().name()?
    )))
}

/// Return "METHOD /path" of the route, for registration errors.
fn route_label(py_meta: &Bound<'_, PyDict>) -> String {
    let part = |key: &str| {
        py_meta
            .get_item(key)
            .ok()
            .flatten()
            .and_then(|v| v.extract::<String>().ok())
            .unwrap_or_else(|| "?".to_string())
    };
    format!("{} {}", part("method"), part("path"))
}

/// Get a required key of the route metadata.
/// A missing key is a ValueError that names the key and the route.
fn required_item<'py>(py_meta: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    py_meta.get_item(key)?.ok_or_else(|| {
        PyValueError::new_err(format!(
            "Route metadata for {} has no '{}' key",
            route_label(py_meta),
            key
        ))
    })
}

/// Make the TypeError for a route metadata value with the wrong type.
fn wrong_type(
    py_meta: &Bound<'_, PyDict>,
    key: &str,
    expected: &str,
    value: &Bound<'_, PyAny>,
) -> PyErr {
    let actual = value
        .get_type()
        .name()
        .map(|name| name.to_string())
        .unwrap_or_else(|_| "?".to_string());
    PyTypeError::new_err(format!(
        "Route metadata '{}' for {} must be {}, got {}",
        key,
        route_label(py_meta),
        expected,
        actual
    ))
}

/// Read a required bool flag of the route metadata.
/// A value that is not a bool is a TypeError.
fn required_bool(py_meta: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    let value = required_item(py_meta, key)?;
    value
        .cast::<PyBool>()
        .map(|flag| flag.is_true())
        .map_err(|_| wrong_type(py_meta, key, "a bool", &value))
}

/// Read a required HTTP status code of the route metadata.
/// A value that is not an int from 0 to 65535 is a TypeError.
fn required_status_code(py_meta: &Bound<'_, PyDict>, key: &str) -> PyResult<u16> {
    let value = required_item(py_meta, key)?;
    if value.is_instance_of::<PyBool>() {
        return Err(wrong_type(py_meta, key, "an int", &value));
    }
    value
        .extract::<u16>()
        .map_err(|_| wrong_type(py_meta, key, "an int from 0 to 65535", &value))
}

impl RouteMetadata {
    /// Parse Python metadata dict into strongly-typed Rust metadata
    pub fn from_python(py_meta: &Bound<'_, PyDict>, py: Python) -> PyResult<Self> {
        let mut auth_backends = Vec::new();
        let mut guards = Vec::new();
        let mut skip: HashSet<String> = HashSet::new();

        // Parse middleware list and extract CORS and rate_limit configs
        let mut cors_config: Option<CorsConfig> = None;
        let mut rate_limit_config: Option<RateLimitConfig> = None;

        if let Ok(Some(mw_list)) = py_meta.get_item("middleware")
            && let Ok(py_list) = mw_list.extract::<Vec<HashMap<String, Py<PyAny>>>>()
        {
            for mw_dict in py_list {
                if let Some(mw_type) = mw_dict.get("type")
                    && let Ok(type_str) = mw_type.extract::<String>(py)
                {
                    // Parse CORS config separately for fast access. The list is
                    // API, router, then route: the last (most specific) config wins.
                    // A config with no origins and no patterns keeps the allow
                    // list of the outer config, so it cannot widen it to the
                    // global settings.
                    if type_str == "cors" {
                        let mut config = parse_cors_config(&mw_dict, py)?;
                        if let Some(outer) = cors_config.take()
                            && config.origins.is_empty()
                            && config.origin_regexes.is_empty()
                        {
                            config.origins = outer.origins;
                            config.origin_set = outer.origin_set;
                            config.allow_all_origins = outer.allow_all_origins;
                            config.origin_regexes = outer.origin_regexes;
                            config.compiled_origin_regexes = outer.compiled_origin_regexes;
                        }
                        cors_config = Some(config);
                    }

                    // Parse rate_limit config separately for fast access
                    if type_str == "rate_limit" && rate_limit_config.is_none() {
                        rate_limit_config = Some(parse_rate_limit_config(&mw_dict, py)?);
                    }
                }
            }
        }

        // Parse auth backends. Failures propagate: metadata that fails to
        // extract must abort startup, not leave the route unauthenticated.
        if let Some(auth_list) = py_meta.get_item("auth_backends")?
            && !auth_list.is_none()
        {
            let py_backends: Vec<HashMap<String, Py<PyAny>>> =
                auth_list.extract().map_err(|e| {
                    PyValueError::new_err(format!("Invalid 'auth_backends' route metadata: {}", e))
                })?;
            for backend_dict in py_backends {
                auth_backends.push(parse_auth_backend(&backend_dict, py)?);
            }
        }

        // Parse guards
        if let Some(guard_list) = py_meta.get_item("guards")?
            && !guard_list.is_none()
        {
            let py_guards: Vec<HashMap<String, Py<PyAny>>> = guard_list.extract().map_err(|e| {
                PyValueError::new_err(format!("Invalid 'guards' route metadata: {}", e))
            })?;
            for guard_dict in py_guards {
                guards.push(parse_guard(&guard_dict, py)?);
            }
        }

        // Parse skip list (e.g., ["compression", "cors"]) into a set
        if let Ok(Some(skip_list)) = py_meta.get_item("skip")
            && let Ok(names) = skip_list.extract::<Vec<String>>()
        {
            for name in names {
                skip.insert(name.to_lowercase());
            }
        }

        // Request flags tell which request parts the handler reads.
        // Python route registration sets each flag, so each one is required.
        let needs_body = required_bool(py_meta, "needs_body")?;
        let needs_query = required_bool(py_meta, "needs_query")?;
        let needs_headers = required_bool(py_meta, "needs_headers")?;
        let needs_cookies = required_bool(py_meta, "needs_cookies")?;
        let needs_path_params = required_bool(py_meta, "needs_path_params")?;
        let is_static_route = required_bool(py_meta, "is_static_route")?;

        // Parse the path and query type hints for Rust-side type coercion
        // Format: {"wire_key": type_hint_id, ...}
        // Type hint IDs match type_coercion.rs constants (TYPE_INT=1, TYPE_FLOAT=2, etc.)
        let path_types = extract_type_hints(py_meta, "path_types");
        let query_types = extract_type_hints(py_meta, "query_types");

        // Header and cookie type hints use the same format, keyed by wire name.
        let header_types = extract_type_hints(py_meta, "header_types");
        let cookie_types = extract_type_hints(py_meta, "cookie_types");

        // Parse form-related metadata for Rust-side form parsing
        let needs_form_parsing = required_bool(py_meta, "needs_form_parsing")?;

        let skip_cors = skip.contains("cors");
        let skip_compression = skip.contains("compression");
        let guards = GuardSet::from_guards(guards);
        let has_auth_or_guards = !auth_backends.is_empty() || guards.is_enforcing();
        let has_rate_limit = rate_limit_config.is_some();
        // An identity key with no backend can never see an identity. Every
        // caller would share the address bucket, so refuse to start.
        if let Some(config) = &rate_limit_config
            && config.key.needs_identity()
            && auth_backends.is_empty()
        {
            let key = match config.key {
                RateLimitKey::User => "user",
                _ => "api_key",
            };
            return Err(PyValueError::new_err(format!(
                "@rate_limit(key=\"{key}\") needs an auth backend on the route or the API; \
                     without one there is no identity to count per. Use key=\"ip\" or a header name."
            )));
        }
        let can_sync_dispatch = py_meta
            .get_item("can_sync_dispatch")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);
        let is_async = py_meta
            .get_item("is_async")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);
        let can_lane_dispatch = py_meta
            .get_item("can_lane_dispatch")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);
        let plan = RouteExecutionPlan::from_parts(
            needs_body,
            needs_query,
            needs_headers,
            needs_cookies,
            needs_form_parsing,
            skip_cors,
            skip_compression,
            has_auth_or_guards,
            has_rate_limit,
            can_sync_dispatch,
            is_async,
            can_lane_dispatch,
        );

        // Form field type hints (same format as path_types)
        let form_type_hints: HashMap<String, u8> = py_meta
            .get_item("form_type_hints")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<HashMap<String, u8>>().ok())
            .unwrap_or_default();

        let form_seq_fields: HashSet<String> = py_meta
            .get_item("form_seq_fields")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<Vec<String>>().ok())
            .map(|v| v.into_iter().collect())
            .unwrap_or_default();

        let query_seq_fields: AHashSet<String> = py_meta
            .get_item("query_seq_fields")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<Vec<String>>().ok())
            .map(|v| v.into_iter().collect())
            .unwrap_or_default();

        // File field constraints
        let file_constraints = parse_file_constraints(py_meta, py);

        // Max upload size (default 1MB)
        let max_upload_size = optional_size(py_meta, "max_upload_size", 1024 * 1024)?;

        // Memory spool threshold - when to spool files to disk (default 1MB)
        let memory_spool_threshold = optional_size(py_meta, "memory_spool_threshold", 1024 * 1024)?;

        // Optional Rust-side argument binding plan.
        let rust_arg_bindings = parse_rust_arg_bindings(py_meta);
        let prebind_only = rust_arg_bindings.is_some()
            && py_meta
                .get_item("prebind_only")
                .ok()
                .flatten()
                .and_then(|v| v.extract::<bool>().ok())
                .unwrap_or(false);

        let websocket_revocation_check = py_meta
            .get_item("websocket_revocation_check")?
            .filter(|value| !value.is_none())
            .map(|value| Arc::new(value.unbind()));

        // Default success status. Python route registration sets it.
        let default_status_code = required_status_code(py_meta, "default_status_code")?;

        Ok(RouteMetadata {
            auth_backends,
            guards,
            skip,
            cors_config,
            rate_limit_config,
            needs_path_params,
            is_static_route,
            path_types,
            query_types,
            header_types,
            cookie_types,
            form_type_hints,
            form_seq_fields,
            query_seq_fields,
            file_constraints,
            max_upload_size,
            memory_spool_threshold,
            rust_arg_bindings,
            prebind_only,
            plan,
            default_status_code,
            websocket_revocation_check,
        })
    }
}

/// Read a list of str from a CORS config. A present key that holds another
/// type stops startup: the route would else use the global allow list.
fn cors_str_list(
    dict: &HashMap<String, Py<PyAny>>,
    py: Python,
    key: &str,
) -> PyResult<Option<Vec<String>>> {
    let Some(value) = dict.get(key).map(|value| value.bind(py)) else {
        return Ok(None);
    };
    if value.is_none() {
        return Ok(None);
    }
    // A str is one item, as for @cors.
    if let Ok(item) = value.extract::<String>() {
        return Ok(Some(vec![item]));
    }
    let type_error =
        || PyTypeError::new_err(format!("cors {key} must be a str or a collection of str"));
    value
        .try_iter()
        .map_err(|_| type_error())?
        .map(|item| {
            item.and_then(|item| item.extract::<String>())
                .map_err(|_| type_error())
        })
        .collect::<PyResult<Vec<String>>>()
        .map(Some)
}

/// Parse CORS configuration from middleware dict
fn parse_cors_config(dict: &HashMap<String, Py<PyAny>>, py: Python) -> PyResult<CorsConfig> {
    let mut config = CorsConfig::default();

    // Parse origins and build origin set for O(1) lookups
    if let Some(origins) = cors_str_list(dict, py, "origins")? {
        // Detect wildcard origin
        config.allow_all_origins = origins.iter().any(|o| o == "*");
        config.origin_set = origins.iter().cloned().collect();
        config.origins = origins;
    }

    // Parse origin_regexes and compile them at startup
    if let Some(regex_patterns) = cors_str_list(dict, py, "origin_regexes")? {
        // An empty pattern matches every origin.
        if regex_patterns.iter().any(String::is_empty) {
            return Err(PyValueError::new_err(
                "cors origin_regexes holds an empty pattern, which matches every origin",
            ));
        }
        config.origin_regexes = regex_patterns.clone();
        config.compiled_origin_regexes = regex_patterns
            .iter()
            .filter_map(|pattern| {
                Regex::new(pattern).ok().or_else(|| {
                    eprintln!(
                        "[django-bolt] Warning: Invalid route-level CORS origin regex pattern: {}",
                        pattern
                    );
                    None
                })
            })
            .collect();
    }

    // Parse credentials
    if let Some(cred_py) = dict.get("credentials")
        && let Ok(cred) = cred_py.extract::<bool>(py)
    {
        config.credentials = cred;
    }

    // Parse methods (optional, has defaults)
    if let Some(methods_py) = dict.get("methods")
        && let Ok(methods) = methods_py.extract::<Vec<String>>(py)
    {
        config.methods_str = methods.join(", ");
        config.methods_header = HeaderValue::from_str(&config.methods_str).ok();
        config.methods = methods;
    }

    // Parse headers (optional, has defaults)
    if let Some(headers_py) = dict.get("headers")
        && let Ok(headers) = headers_py.extract::<Vec<String>>(py)
    {
        config.headers_str = headers.join(", ");
        config.headers_header = HeaderValue::from_str(&config.headers_str).ok();
        config.headers = headers;
    }

    // Parse expose_headers (optional)
    if let Some(expose_py) = dict.get("expose_headers")
        && let Ok(expose) = expose_py.extract::<Vec<String>>(py)
    {
        config.expose_headers_str = expose.join(", ");
        config.expose_headers_header = if !expose.is_empty() {
            HeaderValue::from_str(&config.expose_headers_str).ok()
        } else {
            None
        };
        config.expose_headers = expose;
    }

    // Parse max_age (optional)
    if let Some(age_py) = dict.get("max_age")
        && let Ok(age) = age_py.extract::<u32>(py)
    {
        config.max_age_str = age.to_string();
        config.max_age_header = HeaderValue::from_str(&config.max_age_str).ok();
        config.max_age = age;
    }

    Ok(config)
}

/// Parse rate limiting configuration from middleware dict
fn parse_rate_limit_config(
    dict: &HashMap<String, Py<PyAny>>,
    py: Python,
) -> PyResult<RateLimitConfig> {
    let mut config = RateLimitConfig::default();

    // A value that is present must be valid. A bad value must stop startup,
    // not give the route the default limit.
    if let Some(rps) = rate_limit_count(dict, "rps", py)? {
        config.rps = rps;
    }
    config.burst = match rate_limit_count(dict, "burst", py)? {
        Some(burst) => burst,
        // If burst not specified, default to 2x rps
        None => config.rps.saturating_mul(2),
    };

    // Parse key_type (optional, defaults to "ip").
    // Custom header keys are lowercased ONCE here so the per-request rate-limit
    // check can look them up directly (headers are stored lowercase).
    if let Some(key_py) = dict.get("key") {
        let key_py = key_py.bind(py);
        let key_type = match key_py.extract::<String>() {
            Ok(key_type) if !key_type.is_empty() => key_type,
            _ => {
                return Err(PyTypeError::new_err(format!(
                    "rate_limit key must be a non-empty str, got {}",
                    describe(key_py)
                )));
            }
        };
        config.key = match key_type.to_lowercase().as_str() {
            "ip" => RateLimitKey::Ip,
            "user" => RateLimitKey::User,
            "api_key" => RateLimitKey::ApiKey,
            header => RateLimitKey::Header(header.to_owned()),
        };
    }

    Ok(config)
}

/// Read `rps` or `burst` of a rate limit: None when the key is missing. A bool,
/// a value that is not an int, and an int less than 1 or more than `u32::MAX`
/// raise `TypeError`.
fn rate_limit_count(
    dict: &HashMap<String, Py<PyAny>>,
    key: &str,
    py: Python,
) -> PyResult<Option<u32>> {
    let Some(value) = dict.get(key) else {
        return Ok(None);
    };
    let value = value.bind(py);
    if !value.is_instance_of::<PyBool>()
        && let Ok(count) = value.extract::<u32>()
        && count >= 1
    {
        return Ok(Some(count));
    }
    Err(PyTypeError::new_err(format!(
        "rate_limit {key} must be an int of 1 or more, got {}",
        describe(value)
    )))
}

/// The type name and the repr of a value, for an error message.
fn describe(value: &Bound<'_, PyAny>) -> String {
    let type_name = value
        .get_type()
        .name()
        .map(|name| name.to_string())
        .unwrap_or_else(|_| "?".to_string());
    let repr = value
        .repr()
        .map(|repr| repr.to_string())
        .unwrap_or_else(|_| "?".to_string());
    format!("{type_name} {repr}")
}

/// Parse a single auth backend from Python dict.
///
/// Invalid auth configuration is a hard error, not a skip: silently dropping
/// a backend would leave the route serving without the authentication the
/// user configured.
pub fn parse_auth_backend(dict: &HashMap<String, Py<PyAny>>, py: Python) -> PyResult<AuthBackend> {
    let backend_type = dict
        .get("type")
        .ok_or_else(|| PyValueError::new_err("Auth backend metadata missing 'type'"))?
        .extract::<String>(py)?;

    // Field extraction below is fail-loud: a present-but-mistyped value is a
    // configuration error and propagates; only an absent key (or Python
    // None for optional fields) falls back to the default.
    match backend_type.as_str() {
        "jwt" => {
            let algorithm_names = match dict.get("algorithms") {
                Some(a) => a
                    .extract::<Option<Vec<String>>>(py)?
                    .unwrap_or_else(|| vec!["HS256".to_string()]),
                None => vec!["HS256".to_string()],
            };
            let algorithms = algorithm_names
                .iter()
                .map(|name| parse_jwt_algorithm(name).map_err(PyValueError::new_err))
                .collect::<PyResult<Vec<_>>>()?;
            // Key material is validated and parsed once here — the request
            // path never touches the raw secret/PEM/JWKS again. A JWKS
            // document (fetched by Python from the provider's jwks_url) takes
            // precedence over a single secret when both are present.
            let jwks = match dict.get("jwks") {
                Some(j) => j.extract::<Option<String>>(py)?,
                None => None,
            };
            let keys = if let Some(jwks_json) = jwks {
                let parsed = build_jwks_key_source(&jwks_json).map_err(PyValueError::new_err)?;
                match dict.get("jwks_refresh") {
                    Some(callback) if !callback.is_none(py) => {
                        let interval = match dict.get("jwks_refresh_interval") {
                            Some(value) => value.extract::<Option<u64>>(py)?.unwrap_or(300),
                            None => 300,
                        };
                        let JwtKeySource::Jwks(initial_keys) = parsed else {
                            unreachable!("build_jwks_key_source always returns Jwks")
                        };
                        JwtKeySource::RefreshingJwks(std::sync::Arc::new(RefreshingJwks::new(
                            initial_keys,
                            callback.clone_ref(py),
                            std::time::Duration::from_secs(interval),
                        )))
                    }
                    _ => parsed,
                }
            } else {
                let secret = dict
                    .get("secret")
                    .ok_or_else(|| PyValueError::new_err("JWT auth backend missing 'secret'"))?
                    .extract::<String>(py)?;
                JwtKeySource::Static(StaticKey::new(
                    build_jwt_decoding_key(&secret, &algorithms).map_err(PyValueError::new_err)?,
                ))
            };
            let header = match dict.get("header") {
                Some(h) => h
                    .extract::<Option<String>>(py)?
                    .unwrap_or_else(|| "authorization".to_string()),
                None => "authorization".to_string(),
            };
            let cookie = match dict.get("cookie") {
                Some(c) => c.extract::<Option<String>>(py)?,
                None => None,
            };
            let audience = match dict.get("audience") {
                Some(a) => a.extract::<Option<String>>(py)?,
                None => None,
            };
            let issuer = match dict.get("issuer") {
                Some(i) => i.extract::<Option<String>>(py)?,
                None => None,
            };
            let leeway = match dict.get("leeway") {
                Some(l) => l.extract::<Option<i64>>(py)?.unwrap_or(60),
                None => 60,
            };
            let token_type = match dict.get("token_type") {
                Some(t) => t.extract::<Option<String>>(py)?,
                None => None,
            };
            let require_jti = match dict.get("require_jti") {
                Some(r) => r.extract::<Option<bool>>(py)?.unwrap_or(false),
                None => false,
            };
            let cookie_csrf = match dict.get("cookie_csrf") {
                Some(c) => c.extract::<Option<bool>>(py)?.unwrap_or(true),
                None => true,
            };

            Ok(AuthBackend::JWT {
                keys,
                algorithms,
                header,
                cookie,
                audience,
                issuer,
                leeway,
                token_type,
                require_jti,
                cookie_csrf,
            })
        }
        "api_key" => {
            let api_keys: ApiKeys = match dict.get("api_keys") {
                Some(k) => k
                    .extract::<Option<Vec<String>>>(py)?
                    .unwrap_or_default()
                    .into_iter()
                    .collect(),
                None => ApiKeys::default(),
            };

            let header = match dict.get("header") {
                Some(h) => h
                    .extract::<Option<String>>(py)?
                    .unwrap_or_else(|| "x-api-key".to_string()),
                None => "x-api-key".to_string(),
            };

            let key_permissions = match dict.get("key_permissions") {
                Some(kp) => kp
                    .extract::<Option<HashMap<String, Vec<String>>>>(py)?
                    .unwrap_or_default(),
                None => HashMap::new(),
            };

            Ok(AuthBackend::APIKey {
                api_keys,
                header,
                key_permissions,
            })
        }
        "session" => {
            let cookie = dict
                .get("cookie")
                .ok_or_else(|| PyValueError::new_err("Session auth backend missing 'cookie'"))?
                .extract::<String>(py)?;
            let csrf = match dict.get("csrf") {
                Some(value) => value.extract::<bool>(py)?,
                None => true,
            };
            let login_redirect = match dict.get("login_redirect") {
                Some(value) => value.extract::<bool>(py)?,
                None => false,
            };
            Ok(AuthBackend::Session {
                cookie,
                csrf,
                login_redirect,
            })
        }
        other => Err(PyValueError::new_err(format!(
            "Unknown auth backend type '{}'",
            other
        ))),
    }
}

/// The guards of a route, checked against a user that Django loaded.
///
/// Rust cannot read a Django session. For a session request, Python loads
/// the user through Django and calls `check`. The guards keep one
/// implementation: `evaluate_guards`.
#[pyclass(frozen, module = "django_bolt._core")]
pub struct GuardCheck {
    guards: GuardSet,
}

#[pymethods]
impl GuardCheck {
    /// Build the check once at registration from the compiled guard metadata.
    #[new]
    fn new(py: Python<'_>, guards: Vec<HashMap<String, Py<PyAny>>>) -> PyResult<Self> {
        let guards = guards
            .iter()
            .map(|guard| parse_guard(guard, py))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            guards: GuardSet::from_guards(guards),
        })
    }

    /// Check the guards against the user. `user_id` is None for an anonymous
    /// user. Returns None when the guards allow the request, else the status
    /// and the JSON body of the rejection.
    fn check<'py>(
        &self,
        py: Python<'py>,
        user_id: Option<String>,
        is_staff: bool,
        is_superuser: bool,
        permissions: HashSet<String>,
    ) -> Option<(u16, Bound<'py, PyBytes>)> {
        // An anonymous user has no credential, as a request with no token.
        let ctx = user_id.map(|sub| AuthContext {
            user_id: Some(sub.clone()),
            is_staff,
            is_superuser,
            backend: std::borrow::Cow::Borrowed("session"),
            backend_index: None,
            // `Requires` reads these claims. `permissions` reads the set below.
            claims: Some(Claims {
                sub: Some(sub),
                is_staff: Some(is_staff),
                is_superuser: Some(is_superuser),
                ..Claims::default()
            }),
            permissions,
            cookie_csrf: false,
        });
        match evaluate_guards(&self.guards, ctx.as_ref()) {
            GuardResult::Allow => None,
            GuardResult::Unauthorized => Some((401, PyBytes::new(py, ERROR_BODY_401))),
            GuardResult::Forbidden(denial) => {
                let body = denial
                    .as_deref()
                    .map_or(ERROR_BODY_403, |denial| &denial.body[..]);
                Some((403, PyBytes::new(py, body)))
            }
        }
    }
}

/// Parse a single guard from Python dict
pub fn parse_guard(dict: &HashMap<String, Py<PyAny>>, py: Python) -> PyResult<Guard> {
    let guard_type = dict
        .get("type")
        .ok_or_else(|| PyValueError::new_err("Guard metadata missing 'type'"))?
        .extract::<String>(py)?;

    match guard_type.as_str() {
        "allow_any" => Ok(Guard::AllowAny),
        "is_authenticated" => Ok(Guard::IsAuthenticated),
        // The single permission primitive (`Requires` in Python). The claim
        // name and expected values are extracted here, at registration, so
        // request-time evaluation never touches Python.
        "requires" => {
            let claim = dict
                .get("claim")
                .ok_or_else(|| PyValueError::new_err("requires guard missing 'claim'"))?
                .extract::<String>(py)?;
            if claim.is_empty() {
                return Err(PyValueError::new_err(
                    "requires guard 'claim' must be a non-empty string",
                ));
            }
            let quantifier = match dict
                .get("quantifier")
                .ok_or_else(|| PyValueError::new_err("requires guard missing 'quantifier'"))?
                .extract::<u8>(py)?
            {
                0 => Quantifier::Any,
                1 => Quantifier::All,
                2 => Quantifier::None,
                other => {
                    return Err(PyValueError::new_err(format!(
                        "requires guard '{}' has unknown quantifier {}",
                        claim, other
                    )));
                }
            };
            // Always present (may be None). Serialized to response bytes here,
            // at registration, so a denial costs no formatting per request.
            let denial = dict
                .get("message")
                .ok_or_else(|| PyValueError::new_err("requires guard missing 'message'"))?
                .extract::<Option<String>>(py)?
                .map(|message| Arc::new(GuardDenial::new(message)));
            let values_py = dict
                .get("values")
                .ok_or_else(|| PyValueError::new_err("requires guard missing 'values'"))?
                .extract::<Vec<Py<PyAny>>>(py)?;
            let mut values = Vec::with_capacity(values_py.len());
            for value in &values_py {
                let bound = value.bind(py);
                // bool must be checked before int: Python bool is an int subtype.
                let json = if bound.is_instance_of::<pyo3::types::PyBool>() {
                    serde_json::Value::Bool(bound.extract::<bool>()?)
                } else if let Ok(i) = bound.extract::<i64>() {
                    serde_json::Value::from(i)
                } else if let Ok(f) = bound.extract::<f64>() {
                    serde_json::Number::from_f64(f)
                        .map(serde_json::Value::Number)
                        .ok_or_else(|| {
                            PyValueError::new_err(format!(
                                "requires guard '{}' has a non-finite float value",
                                claim
                            ))
                        })?
                } else if let Ok(s) = bound.extract::<String>() {
                    serde_json::Value::String(s)
                } else {
                    return Err(PyValueError::new_err(format!(
                        "requires guard '{}' values must be str, int, float, or bool",
                        claim
                    )));
                };
                values.push(json);
            }
            Ok(Guard::Requires {
                claim: ClaimKey::parse(claim),
                values,
                quantifier,
                denial,
            })
        }
        other => Err(PyValueError::new_err(format!(
            "Unknown guard type '{}'",
            other
        ))),
    }
}

/// Parse file field constraints from Python metadata
/// Format: {"field_name": {"max_size": 1024, "min_size": 0, "allowed_types": ["image/*"], "max_files": 5}, ...}
fn parse_file_constraints(
    py_meta: &Bound<'_, PyDict>,
    py: Python,
) -> HashMap<String, FileFieldConstraints> {
    let mut result = HashMap::new();

    if let Ok(Some(constraints_dict)) = py_meta.get_item("file_constraints")
        && let Ok(constraints_map) =
            constraints_dict.extract::<HashMap<String, HashMap<String, Py<PyAny>>>>()
    {
        for (field_name, field_constraints) in constraints_map {
            let mut constraints = FileFieldConstraints::default();

            if let Some(max_size_py) = field_constraints.get("max_size")
                && let Ok(max_size) = max_size_py.extract::<usize>(py)
            {
                constraints.max_size = Some(max_size);
            }

            if let Some(min_size_py) = field_constraints.get("min_size")
                && let Ok(min_size) = min_size_py.extract::<usize>(py)
            {
                constraints.min_size = Some(min_size);
            }

            if let Some(allowed_types_py) = field_constraints.get("allowed_types")
                && let Ok(allowed_types) = allowed_types_py.extract::<Vec<String>>(py)
            {
                constraints.allowed_types = Some(allowed_types);
            }

            if let Some(max_files_py) = field_constraints.get("max_files")
                && let Ok(max_files) = max_files_py.extract::<usize>(py)
            {
                constraints.max_files = Some(max_files);
            }

            result.insert(field_name, constraints);
        }
    }

    result
}

/// Parse Rust-side argument binding plan from Python metadata.
///
/// Expected format:
/// [
///   {"source": "path|query|header|cookie", "lookup_key": "...", "arg_name": "...",
///    "arg_kind": "positional|keyword", "required": bool, "inject_none": bool},
///   ...
/// ]
///
/// `required` defaults to true and `inject_none` to false so older metadata
/// shapes keep the strict all-or-nothing behavior.
/// Parse a `{"name": type_hint_id}` dict into `TypeHints`.
/// Type hint IDs match type_coercion.rs constants (TYPE_INT=1, TYPE_FLOAT=2, etc.)
fn extract_type_hints(py_meta: &Bound<'_, PyDict>, key: &str) -> TypeHints {
    py_meta
        .get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<HashMap<String, u8>>().ok())
        .map(|hints| hints.into_iter().collect())
        .unwrap_or_default()
}

fn parse_rust_arg_bindings(py_meta: &Bound<'_, PyDict>) -> Option<Vec<RustArgBinding>> {
    let py = py_meta.py();
    let bindings_obj = py_meta.get_item("rust_arg_bindings").ok().flatten()?;
    let py_list = bindings_obj.cast::<PyList>().ok()?;

    let mut bindings = Vec::with_capacity(py_list.len());

    for item in py_list.iter() {
        let entry = item.cast::<PyDict>().ok()?;

        let source = match entry
            .get_item("source")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<String>().ok())
            .as_deref()
        {
            Some("path") => RustArgSource::Path,
            Some("query") => RustArgSource::Query,
            Some("header") => RustArgSource::Header,
            Some("cookie") => RustArgSource::Cookie,
            _ => return None,
        };

        let lookup_key = entry
            .get_item("lookup_key")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<String>().ok())?;
        if lookup_key.is_empty() {
            return None;
        }

        let arg_name = entry
            .get_item("arg_name")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<String>().ok())?;
        if arg_name.is_empty() {
            return None;
        }

        let positional = match entry
            .get_item("arg_kind")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<String>().ok())
            .as_deref()
        {
            Some("positional") => true,
            Some("keyword") => false,
            _ => return None,
        };

        let required = entry
            .get_item("required")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let inject_none = entry
            .get_item("inject_none")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);

        // Optional params must be keyword-bound: a skipped positional slot
        // would shift every following positional argument.
        if !required && positional {
            return None;
        }

        // Intern once at registration — per-request lookups reuse the object.
        bindings.push(RustArgBinding {
            source,
            key: lookup_key,
            arg_name: PyString::intern(py, &arg_name).unbind(),
            positional,
            required,
            inject_none,
        });
    }

    if bindings.is_empty() {
        None
    } else {
        Some(bindings)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::auth::authenticate;
    use base64::Engine;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use jsonwebtoken::{Algorithm, EncodingKey, crypto, get_current_timestamp};
    use std::ffi::CString;
    use std::time::{Duration, Instant};

    fn with_py<R>(test: impl FnOnce(Python<'_>) -> R) -> R {
        Python::initialize();
        Python::attach(test)
    }

    /// Evaluate a Python expression. `vars` are the names that it can read.
    fn eval<'py>(
        py: Python<'py>,
        code: &str,
        vars: &[(&str, Bound<'py, PyAny>)],
    ) -> Bound<'py, PyAny> {
        let globals = PyDict::new(py);
        for (name, value) in vars {
            globals.set_item(name, value).unwrap();
        }
        py.eval(&CString::new(code).unwrap(), Some(&globals), None)
            .unwrap_or_else(|e| panic!("bad test code {code:?}: {e}"))
    }

    fn py_dict<'py>(py: Python<'py>, code: &str) -> Bound<'py, PyDict> {
        eval(py, code, &[]).cast_into::<PyDict>().unwrap()
    }

    /// A middleware, backend or guard dict, as the parsers take it.
    fn map(py: Python<'_>, code: &str) -> HashMap<String, Py<PyAny>> {
        py_dict(py, code).extract().unwrap()
    }

    /// The text of the error: the exception class, a colon and the message.
    fn error_of<T>(result: PyResult<T>) -> String {
        result.err().expect("the call must fail").to_string()
    }

    // ---- RouteExecutionPlan ----

    /// The plan in the order of the arguments of `from_parts`.
    fn readings(plan: RouteExecutionPlan) -> [bool; 12] {
        [
            plan.needs_body(),
            plan.needs_query(),
            plan.needs_headers(),
            plan.needs_cookies(),
            plan.needs_form_parsing(),
            plan.skip_cors(),
            plan.skip_compression(),
            plan.has_auth_or_guards(),
            plan.has_rate_limit(),
            plan.can_sync_dispatch(),
            plan.is_async(),
            plan.can_lane_dispatch(),
        ]
    }

    fn only(index: usize) -> [bool; 12] {
        let mut flags = [false; 12];
        flags[index] = true;
        flags
    }

    fn plan_of(f: [bool; 12]) -> RouteExecutionPlan {
        RouteExecutionPlan::from_parts(
            f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], f[10], f[11],
        )
    }

    #[test]
    fn an_empty_plan_has_no_flag() {
        assert_eq!(readings(plan_of([false; 12])), [false; 12]);
        assert_eq!(readings(RouteExecutionPlan::default()), [false; 12]);
    }

    #[test]
    fn each_plan_argument_sets_only_its_own_flag() {
        for index in 0..12 {
            assert_eq!(readings(plan_of(only(index))), only(index), "flag {index}");
        }
    }

    #[test]
    fn a_plan_keeps_all_flags_when_all_are_set() {
        assert_eq!(readings(plan_of([true; 12])), [true; 12]);
    }

    // ---- RateLimitKey and the config defaults ----

    #[test]
    fn only_an_identity_key_needs_the_auth_context() {
        assert!(!RateLimitKey::Ip.needs_identity());
        assert!(!RateLimitKey::Header("x-tenant".into()).needs_identity());
        assert!(RateLimitKey::User.needs_identity());
        assert!(RateLimitKey::ApiKey.needs_identity());
    }

    #[test]
    fn the_default_rate_limit_is_100_per_second_with_burst_200_by_address() {
        let config = RateLimitConfig::default();
        assert_eq!((config.rps, config.burst), (100, 200));
        assert_eq!(config.key, RateLimitKey::Ip);
    }

    #[test]
    fn the_default_cors_config_allows_no_origin_and_sets_the_header_values() {
        let config = CorsConfig::default();
        assert!(config.origins.is_empty() && config.origin_regexes.is_empty());
        assert!(config.compiled_origin_regexes.is_empty() && config.origin_set.is_empty());
        assert!(!config.allow_all_origins && !config.credentials);
        assert_eq!(
            config.methods,
            ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "QUERY"]
        );
        assert_eq!(
            config.methods_str,
            "GET, POST, PUT, PATCH, DELETE, OPTIONS, QUERY"
        );
        assert_eq!(config.headers, ["Content-Type", "Authorization"]);
        assert_eq!(config.headers_str, "Content-Type, Authorization");
        assert!(config.expose_headers.is_empty());
        assert_eq!(config.expose_headers_str, "");
        assert_eq!(
            (config.max_age, config.max_age_str.as_str()),
            (3600, "3600")
        );
        assert_eq!(config.methods_header.unwrap(), config.methods_str.as_str());
        assert_eq!(config.headers_header.unwrap(), config.headers_str.as_str());
        assert!(config.expose_headers_header.is_none());
        assert_eq!(config.max_age_header.unwrap(), "3600");
    }

    #[test]
    fn the_default_compression_config_is_brotli_level_5() {
        let config = CompressionConfig::default();
        assert_eq!(config.backend, "brotli");
        assert_eq!(config.minimum_size, 500);
        assert!(config.gzip_fallback);
        assert_eq!(
            (
                config.brotli_level,
                config.brotli_lgwin,
                config.gzip_level,
                config.zstd_level
            ),
            (5, 14, 6, 3)
        );
    }

    // ---- CorsConfig::from_django_settings ----

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| item.to_string()).collect()
    }

    #[test]
    fn django_settings_with_only_origins_keep_the_default_header_values() {
        let config = CorsConfig::from_django_settings(
            strings(&["https://a.test", "https://b.test"]),
            vec![],
            false,
            false,
            None,
            None,
            None,
            None,
        );
        let default = CorsConfig::default();
        assert_eq!(config.origins, ["https://a.test", "https://b.test"]);
        assert!(config.origin_set.contains("https://a.test"));
        assert!(config.origin_set.contains("https://b.test"));
        assert_eq!(config.origin_set.len(), 2);
        assert!(!config.allow_all_origins && !config.credentials);
        assert_eq!(config.methods, default.methods);
        assert_eq!(config.methods_str, default.methods_str);
        assert_eq!(config.headers, default.headers);
        assert_eq!(config.headers_str, default.headers_str);
        assert!(config.expose_headers.is_empty() && config.expose_headers_header.is_none());
        assert_eq!(config.max_age, 3600);
        assert_eq!(config.max_age_str, "3600");
        assert_eq!(config.max_age_header.unwrap(), "3600");
    }

    #[test]
    fn django_settings_set_the_flags_and_the_listed_header_values() {
        let config = CorsConfig::from_django_settings(
            strings(&["*"]),
            vec![],
            true,
            true,
            Some(strings(&["GET", "POST"])),
            Some(strings(&["X-One", "X-Two"])),
            Some(strings(&["X-Out"])),
            Some(120),
        );
        assert!(config.allow_all_origins && config.credentials);
        assert_eq!(config.methods, ["GET", "POST"]);
        assert_eq!(config.methods_str, "GET, POST");
        assert_eq!(config.methods_header.unwrap(), "GET, POST");
        assert_eq!(config.headers, ["X-One", "X-Two"]);
        assert_eq!(config.headers_str, "X-One, X-Two");
        assert_eq!(config.headers_header.unwrap(), "X-One, X-Two");
        assert_eq!(config.expose_headers, ["X-Out"]);
        assert_eq!(config.expose_headers_str, "X-Out");
        assert_eq!(config.expose_headers_header.unwrap(), "X-Out");
        assert_eq!((config.max_age, config.max_age_str.as_str()), (120, "120"));
        assert_eq!(config.max_age_header.unwrap(), "120");
    }

    #[test]
    fn django_settings_with_empty_expose_headers_send_no_expose_header() {
        let config = CorsConfig::from_django_settings(
            vec![],
            vec![],
            false,
            false,
            None,
            None,
            Some(vec![]),
            Some(0),
        );
        assert!(config.expose_headers.is_empty());
        assert_eq!(config.expose_headers_str, "");
        assert!(config.expose_headers_header.is_none());
        assert_eq!((config.max_age, config.max_age_str.as_str()), (0, "0"));
        assert_eq!(config.max_age_header.unwrap(), "0");
    }

    #[test]
    fn django_settings_compile_each_valid_origin_regex_and_drop_an_invalid_one() {
        let patterns = strings(&[r"^https://\w+\.example\.com$", "(unclosed", r"^http://dev$"]);
        let config = CorsConfig::from_django_settings(
            vec![],
            patterns.clone(),
            false,
            false,
            None,
            None,
            None,
            None,
        );
        // The strings keep all patterns. Only the valid ones compile.
        assert_eq!(config.origin_regexes, patterns);
        assert_eq!(config.compiled_origin_regexes.len(), 2);
        assert!(config.compiled_origin_regexes[0].is_match("https://api.example.com"));
        assert!(!config.compiled_origin_regexes[0].is_match("https://evil.test"));
        assert!(config.compiled_origin_regexes[1].is_match("http://dev"));
    }

    // ---- parse_cors_config ----

    fn cors(code: &str) -> PyResult<CorsConfig> {
        with_py(|py| parse_cors_config(&map(py, code), py))
    }

    #[test]
    fn a_cors_dict_with_no_key_gives_the_default_config() {
        let config = cors("{'type': 'cors'}").unwrap();
        let default = CorsConfig::default();
        assert!(config.origins.is_empty() && config.origin_regexes.is_empty());
        assert!(!config.allow_all_origins && !config.credentials);
        assert_eq!(config.methods_str, default.methods_str);
        assert_eq!(config.headers_str, default.headers_str);
        assert_eq!(config.max_age, 3600);
    }

    #[test]
    fn cors_origins_fill_the_list_and_the_set() {
        let config = cors("{'origins': ['https://a.test', 'https://b.test']}").unwrap();
        assert_eq!(config.origins, ["https://a.test", "https://b.test"]);
        assert_eq!(config.origin_set.len(), 2);
        assert!(config.origin_set.contains("https://a.test"));
        assert!(config.origin_set.contains("https://b.test"));
        assert!(!config.allow_all_origins);
    }

    #[test]
    fn a_wildcard_origin_allows_all_origins() {
        let config = cors("{'origins': ['https://a.test', '*']}").unwrap();
        assert!(config.allow_all_origins);
        assert!(config.origin_set.contains("*"));
    }

    #[test]
    fn a_cors_origin_str_is_one_origin() {
        let config = cors("{'origins': 'https://a.test'}").unwrap();
        assert_eq!(config.origins, ["https://a.test"]);
        assert!(config.origin_set.contains("https://a.test"));
    }

    #[test]
    fn cors_origins_can_be_any_collection_of_str() {
        let config =
            cors("{'origins': ('https://a.test',), 'origin_regexes': {'^http://x$'}}").unwrap();
        assert_eq!(config.origins, ["https://a.test"]);
        assert_eq!(config.origin_regexes, ["^http://x$"]);
    }

    #[test]
    fn cors_origins_of_none_keep_the_default() {
        let config = cors("{'origins': None, 'origin_regexes': None}").unwrap();
        assert!(config.origins.is_empty() && config.origin_regexes.is_empty());
        assert!(config.origin_set.is_empty());
    }

    #[test]
    fn cors_origins_of_the_wrong_type_stop_startup() {
        assert_eq!(
            error_of(cors("{'origins': 5}")),
            "TypeError: cors origins must be a str or a collection of str"
        );
        assert_eq!(
            error_of(cors("{'origins': ['https://a.test', 5]}")),
            "TypeError: cors origins must be a str or a collection of str"
        );
        assert_eq!(
            error_of(cors("{'origin_regexes': 5}")),
            "TypeError: cors origin_regexes must be a str or a collection of str"
        );
        assert_eq!(
            error_of(cors("{'origin_regexes': [b'x']}")),
            "TypeError: cors origin_regexes must be a str or a collection of str"
        );
    }

    #[test]
    fn an_empty_origin_regex_stops_startup() {
        assert_eq!(
            error_of(cors("{'origin_regexes': ['^ok$', '']}")),
            "ValueError: cors origin_regexes holds an empty pattern, which matches every origin"
        );
    }

    #[test]
    fn cors_origin_regexes_compile_and_an_invalid_one_is_dropped() {
        let config =
            cors(r"{'origin_regexes': ['^https://\\w+\\.example\\.com$', '(unclosed']}").unwrap();
        assert_eq!(config.origin_regexes.len(), 2);
        assert_eq!(config.compiled_origin_regexes.len(), 1);
        assert!(config.compiled_origin_regexes[0].is_match("https://api.example.com"));
        assert!(!config.compiled_origin_regexes[0].is_match("https://example.org"));
    }

    #[test]
    fn cors_credentials_follow_a_bool() {
        assert!(cors("{'credentials': True}").unwrap().credentials);
        assert!(!cors("{'credentials': False}").unwrap().credentials);
        assert!(!cors("{}").unwrap().credentials);
    }

    #[test]
    fn cors_methods_headers_and_expose_headers_set_the_text_and_the_header_value() {
        let config = cors(
            "{'methods': ['GET', 'PUT'], 'headers': ['X-A', 'X-B'], 'expose_headers': ['X-C', 'X-D']}",
        )
        .unwrap();
        assert_eq!(config.methods, ["GET", "PUT"]);
        assert_eq!(config.methods_str, "GET, PUT");
        assert_eq!(config.methods_header.unwrap(), "GET, PUT");
        assert_eq!(config.headers, ["X-A", "X-B"]);
        assert_eq!(config.headers_str, "X-A, X-B");
        assert_eq!(config.headers_header.unwrap(), "X-A, X-B");
        assert_eq!(config.expose_headers, ["X-C", "X-D"]);
        assert_eq!(config.expose_headers_str, "X-C, X-D");
        assert_eq!(config.expose_headers_header.unwrap(), "X-C, X-D");
    }

    #[test]
    fn empty_cors_expose_headers_send_no_header_and_empty_methods_stay_empty() {
        let config = cors("{'expose_headers': [], 'methods': [], 'headers': []}").unwrap();
        assert!(config.expose_headers.is_empty());
        assert_eq!(config.expose_headers_str, "");
        assert!(config.expose_headers_header.is_none());
        assert!(config.methods.is_empty() && config.headers.is_empty());
        assert_eq!(config.methods_str, "");
        assert_eq!(config.headers_str, "");
    }

    #[test]
    fn cors_headers_of_none_keep_the_default() {
        // `@cors` passes `headers=None` when the route sets no headers.
        let config = cors("{'headers': None, 'methods': None, 'expose_headers': None}").unwrap();
        let default = CorsConfig::default();
        assert_eq!(config.headers_str, default.headers_str);
        assert_eq!(config.methods_str, default.methods_str);
        assert!(config.expose_headers.is_empty());
        assert!(config.expose_headers_header.is_none());
    }

    #[test]
    fn cors_max_age_sets_the_number_the_text_and_the_header_value() {
        let config = cors("{'max_age': 86400}").unwrap();
        assert_eq!(config.max_age, 86400);
        assert_eq!(config.max_age_str, "86400");
        assert_eq!(config.max_age_header.unwrap(), "86400");
        let zero = cors("{'max_age': 0}").unwrap();
        assert_eq!((zero.max_age, zero.max_age_str.as_str()), (0, "0"));
    }

    // ---- parse_rate_limit_config ----

    fn rate_limit(code: &str) -> RateLimitConfig {
        with_py(|py| parse_rate_limit_config(&map(py, code), py)).expect("a rate limit config")
    }

    #[test]
    fn a_rate_limit_takes_the_rps_burst_and_key_of_the_dict() {
        let config = rate_limit("{'type': 'rate_limit', 'rps': 10, 'burst': 25, 'key': 'user'}");
        assert_eq!((config.rps, config.burst), (10, 25));
        assert_eq!(config.key, RateLimitKey::User);
    }

    #[test]
    fn a_rate_limit_without_burst_gets_twice_the_rps() {
        let config = rate_limit("{'rps': 7}");
        assert_eq!((config.rps, config.burst), (7, 14));
    }

    #[test]
    fn a_rate_limit_without_rps_keeps_the_default_rps() {
        let config = rate_limit("{'burst': 5}");
        assert_eq!((config.rps, config.burst), (100, 5));
        let none = rate_limit("{}");
        assert_eq!((none.rps, none.burst), (100, 200));
    }

    #[test]
    fn a_rate_limit_key_is_a_known_name_or_a_lowercase_header() {
        assert_eq!(rate_limit("{'key': 'ip'}").key, RateLimitKey::Ip);
        assert_eq!(rate_limit("{'key': 'IP'}").key, RateLimitKey::Ip);
        assert_eq!(rate_limit("{'key': 'user'}").key, RateLimitKey::User);
        assert_eq!(rate_limit("{'key': 'User'}").key, RateLimitKey::User);
        assert_eq!(rate_limit("{'key': 'api_key'}").key, RateLimitKey::ApiKey);
        assert_eq!(rate_limit("{'key': 'API_KEY'}").key, RateLimitKey::ApiKey);
        assert_eq!(
            rate_limit("{'key': 'X-Tenant-ID'}").key,
            RateLimitKey::Header("x-tenant-id".to_string())
        );
    }

    fn rate_limit_error(code: &str) -> String {
        with_py(|py| {
            parse_rate_limit_config(&map(py, code), py)
                .expect_err("a bad rate limit value")
                .to_string()
        })
    }

    /// A bad value stops startup. Before, the route got 100 rps and burst 200.
    #[test]
    fn a_rate_limit_with_a_bad_count_is_an_error() {
        for (code, got) in [
            ("{'rps': 0.5, 'burst': 1}", "float 0.5"),
            ("{'rps': '10'}", "str '10'"),
            ("{'rps': True}", "bool True"),
            ("{'rps': 0}", "int 0"),
            ("{'rps': -5}", "int -5"),
            ("{'rps': 2**32}", "int 4294967296"),
        ] {
            assert_eq!(
                rate_limit_error(code),
                format!("TypeError: rate_limit rps must be an int of 1 or more, got {got}"),
                "{code}"
            );
        }
        assert_eq!(
            rate_limit_error("{'rps': 10, 'burst': 0}"),
            "TypeError: rate_limit burst must be an int of 1 or more, got int 0"
        );
        assert_eq!(
            rate_limit_error("{'rps': 10, 'burst': None}"),
            "TypeError: rate_limit burst must be an int of 1 or more, got NoneType None"
        );
    }

    #[test]
    fn a_rate_limit_key_that_is_not_a_non_empty_str_is_an_error() {
        assert_eq!(
            rate_limit_error("{'key': 5}"),
            "TypeError: rate_limit key must be a non-empty str, got int 5"
        );
        assert_eq!(
            rate_limit_error("{'key': ''}"),
            "TypeError: rate_limit key must be a non-empty str, got str ''"
        );
    }

    #[test]
    fn a_rate_limit_of_the_largest_rps_keeps_a_burst_that_fits() {
        let config = rate_limit("{'rps': 4294967295}");
        assert_eq!((config.rps, config.burst), (u32::MAX, u32::MAX));
    }

    #[test]
    fn a_route_with_a_bad_rate_limit_is_an_error() {
        let err = parse_route("'middleware': [{'type': 'rate_limit', 'rps': 0.5}]").unwrap_err();
        assert_eq!(
            err.to_string(),
            "TypeError: rate_limit rps must be an int of 1 or more, got float 0.5"
        );
    }

    // ---- RouteMetadata::from_python ----

    const REQUIRED: &str = "'method': 'GET', 'path': '/x', 'needs_body': False, \
        'needs_query': False, 'needs_headers': False, 'needs_cookies': False, \
        'needs_path_params': False, 'is_static_route': False, \
        'needs_form_parsing': False, 'default_status_code': 200";

    /// The metadata dict that Python registration builds, plus `extra`.
    /// A key in `extra` replaces the key of the base.
    fn route_dict<'py>(py: Python<'py>, extra: &str) -> Bound<'py, PyDict> {
        py_dict(py, &format!("{{{REQUIRED}, {extra}}}"))
    }

    fn parse_route(extra: &str) -> PyResult<RouteMetadata> {
        with_py(|py| RouteMetadata::from_python(&route_dict(py, extra), py))
    }

    fn route(extra: &str) -> RouteMetadata {
        parse_route(extra).unwrap()
    }

    fn parse_route_without(key: &str) -> PyResult<RouteMetadata> {
        with_py(|py| {
            let dict = route_dict(py, "");
            dict.del_item(key).unwrap();
            RouteMetadata::from_python(&dict, py)
        })
    }

    const FLAG_KEYS: [&str; 6] = [
        "needs_body",
        "needs_query",
        "needs_headers",
        "needs_cookies",
        "needs_path_params",
        "is_static_route",
    ];

    #[test]
    fn a_route_with_every_flag_off_has_an_empty_plan() {
        let meta = route("");
        assert_eq!(readings(meta.plan), [false; 12]);
        assert!(!meta.needs_path_params && !meta.is_static_route);
        assert_eq!(meta.default_status_code, 200);
        assert!(meta.auth_backends.is_empty() && meta.guards.is_empty());
        assert!(meta.skip.is_empty());
        assert!(meta.cors_config.is_none() && meta.rate_limit_config.is_none());
        assert!(meta.rust_arg_bindings.is_none() && !meta.prebind_only);
        assert!(meta.websocket_revocation_check.is_none());
        assert_eq!(meta.max_upload_size, 1024 * 1024);
        assert_eq!(meta.memory_spool_threshold, 1024 * 1024);
    }

    #[test]
    fn a_request_flag_of_the_route_sets_only_its_plan_flag() {
        for (index, key) in [
            "needs_body",
            "needs_query",
            "needs_headers",
            "needs_cookies",
            "needs_form_parsing",
        ]
        .into_iter()
        .enumerate()
        {
            let meta = route(&format!("'{key}': True"));
            assert_eq!(readings(meta.plan), only(index), "{key}");
        }
    }

    #[test]
    fn the_path_param_and_static_flags_are_not_in_the_plan() {
        let meta = route("'needs_path_params': True");
        assert!(meta.needs_path_params && !meta.is_static_route);
        let meta = route("'is_static_route': True");
        assert!(!meta.needs_path_params && meta.is_static_route);
    }

    #[test]
    fn a_missing_required_key_is_a_value_error_that_names_the_key_and_the_route() {
        for key in FLAG_KEYS
            .into_iter()
            .chain(["needs_form_parsing", "default_status_code"])
        {
            assert_eq!(
                error_of(parse_route_without(key)),
                format!("ValueError: Route metadata for GET /x has no '{key}' key"),
                "{key}"
            );
        }
    }

    #[test]
    fn an_error_names_a_route_with_no_method_or_path_by_question_marks() {
        let text = with_py(|py| {
            let dict = route_dict(py, "'method': 5");
            dict.del_item("path").unwrap();
            dict.del_item("needs_body").unwrap();
            error_of(RouteMetadata::from_python(&dict, py))
        });
        assert_eq!(
            text,
            "ValueError: Route metadata for ? ? has no 'needs_body' key"
        );
    }

    #[test]
    fn a_flag_that_is_not_a_bool_is_a_type_error() {
        for key in FLAG_KEYS.into_iter().chain(["needs_form_parsing"]) {
            for (value, name) in [("1", "int"), ("'yes'", "str"), ("None", "NoneType")] {
                assert_eq!(
                    error_of(parse_route(&format!("'{key}': {value}"))),
                    format!(
                        "TypeError: Route metadata '{key}' for GET /x must be a bool, got {name}"
                    ),
                    "{key} = {value}"
                );
            }
        }
    }

    #[test]
    fn the_default_status_code_is_an_int_that_fits_a_u16() {
        assert_eq!(route("'default_status_code': 201").default_status_code, 201);
        assert_eq!(
            error_of(parse_route("'default_status_code': True")),
            "TypeError: Route metadata 'default_status_code' for GET /x must be an int, got bool"
        );
        for (value, name) in [
            ("65536", "int"),
            ("-1", "int"),
            ("'200'", "str"),
            ("200.0", "float"),
        ] {
            assert_eq!(
                error_of(parse_route(&format!("'default_status_code': {value}"))),
                format!(
                    "TypeError: Route metadata 'default_status_code' for GET /x must be an int from 0 to 65535, got {name}"
                ),
                "{value}"
            );
        }
    }

    #[test]
    fn the_upload_sizes_default_to_one_megabyte_and_take_an_int_of_zero_or_more() {
        let meta = route("'max_upload_size': 10, 'memory_spool_threshold': 20");
        assert_eq!(
            (meta.max_upload_size, meta.memory_spool_threshold),
            (10, 20)
        );
        let meta = route("'max_upload_size': 0, 'memory_spool_threshold': 0");
        assert_eq!((meta.max_upload_size, meta.memory_spool_threshold), (0, 0));
        // A key that is None is not "missing": it is the wrong type.
        for key in ["max_upload_size", "memory_spool_threshold"] {
            for (value, name) in [
                ("True", "bool"),
                ("-1", "int"),
                ("'1'", "str"),
                ("None", "NoneType"),
            ] {
                assert_eq!(
                    error_of(parse_route(&format!("'{key}': {value}"))),
                    format!(
                        "TypeError: Route metadata '{key}' must be an int of 0 or more, got {name}"
                    ),
                    "{key} = {value}"
                );
            }
        }
    }

    #[test]
    fn the_skip_list_is_stored_in_lowercase_and_sets_the_skip_flags() {
        let meta = route("'skip': ['CORS', 'Compression', 'Custom']");
        assert_eq!(meta.skip.len(), 3);
        assert!(meta.skip.contains("cors") && meta.skip.contains("compression"));
        assert!(meta.skip.contains("custom"));
        assert_eq!(readings(meta.plan)[5..7], [true, true]);

        assert_eq!(readings(route("'skip': ['cors']").plan), only(5));
        assert_eq!(readings(route("'skip': ['compression']").plan), only(6));
        assert_eq!(readings(route("'skip': ['other']").plan), [false; 12]);
        // A skip value that is not a list of str is ignored.
        assert!(route("'skip': 'cors'").skip.is_empty());
        assert!(route("'skip': None").skip.is_empty());
    }

    #[test]
    fn the_dispatch_flags_are_optional_bools() {
        assert_eq!(readings(route("'can_sync_dispatch': True").plan), only(9));
        assert_eq!(readings(route("'is_async': True").plan), only(10));
        assert_eq!(readings(route("'can_lane_dispatch': True").plan), only(11));
        let off = route("'can_sync_dispatch': 'yes', 'is_async': 1, 'can_lane_dispatch': None");
        assert_eq!(readings(off.plan), [false; 12]);
        let all = route("'can_sync_dispatch': True, 'is_async': True, 'can_lane_dispatch': True");
        assert_eq!(readings(all.plan)[9..], [true, true, true]);
    }

    #[test]
    fn the_type_hints_of_each_source_stay_in_their_own_map() {
        let meta = route(
            "'path_types': {'id': 1}, 'query_types': {'page': 2}, \
             'header_types': {'x-count': 3}, 'cookie_types': {'visits': 4}",
        );
        assert_eq!(meta.path_types.len(), 1);
        assert_eq!(meta.path_types["id"], 1);
        assert_eq!(meta.query_types.len(), 1);
        assert_eq!(meta.query_types["page"], 2);
        assert_eq!(meta.header_types.len(), 1);
        assert_eq!(meta.header_types["x-count"], 3);
        assert_eq!(meta.cookie_types.len(), 1);
        assert_eq!(meta.cookie_types["visits"], 4);
        let none = route("'path_types': 5, 'query_types': {'a': 'b'}");
        assert!(none.path_types.is_empty() && none.query_types.is_empty());
        assert!(none.header_types.is_empty() && none.cookie_types.is_empty());
    }

    #[test]
    fn the_form_and_query_sequence_metadata_is_read() {
        let meta = route(
            "'form_type_hints': {'age': 1, 'tags': 5}, 'form_seq_fields': ['tags', 'ids'], \
             'query_seq_fields': ['q']",
        );
        assert_eq!(meta.form_type_hints.len(), 2);
        assert_eq!(meta.form_type_hints["age"], 1);
        assert_eq!(meta.form_type_hints["tags"], 5);
        assert_eq!(meta.form_seq_fields.len(), 2);
        assert!(meta.form_seq_fields.contains("tags") && meta.form_seq_fields.contains("ids"));
        assert_eq!(meta.query_seq_fields.len(), 1);
        assert!(meta.query_seq_fields.contains("q"));
        assert!(!meta.form_seq_fields.contains("q"));
        let bad = route("'form_type_hints': 1, 'form_seq_fields': 2, 'query_seq_fields': 3");
        assert!(bad.form_type_hints.is_empty());
        assert!(bad.form_seq_fields.is_empty() && bad.query_seq_fields.is_empty());
    }

    #[test]
    fn the_file_constraints_of_the_route_are_parsed() {
        let meta =
            route("'file_constraints': {'avatar': {'max_size': 10, 'allowed_types': ['image/*']}}");
        assert_eq!(meta.file_constraints.len(), 1);
        assert_eq!(meta.file_constraints["avatar"].max_size, Some(10));
    }

    #[test]
    fn the_revocation_check_is_the_python_callable_or_none() {
        assert!(route("").websocket_revocation_check.is_none());
        assert!(
            route("'websocket_revocation_check': None")
                .websocket_revocation_check
                .is_none()
        );
        with_py(|py| {
            let check = eval(py, "lambda auth: False", &[]);
            let dict = route_dict(py, "");
            dict.set_item("websocket_revocation_check", &check).unwrap();
            let meta = RouteMetadata::from_python(&dict, py).unwrap();
            let stored = meta.websocket_revocation_check.unwrap();
            assert!(stored.bind(py).is(&check));
        });
    }

    #[test]
    fn the_rust_arg_bindings_are_read_and_prebind_only_needs_them() {
        let binding =
            "{'source': 'query', 'lookup_key': 'q', 'arg_name': 'q', 'arg_kind': 'keyword'}";
        let meta = route(&format!("'rust_arg_bindings': [{binding}]"));
        assert_eq!(meta.rust_arg_bindings.as_ref().unwrap().len(), 1);
        assert!(!meta.prebind_only);
        let meta = route(&format!(
            "'rust_arg_bindings': [{binding}], 'prebind_only': True"
        ));
        assert!(meta.prebind_only);
        let meta = route(&format!(
            "'rust_arg_bindings': [{binding}], 'prebind_only': 'yes'"
        ));
        assert!(!meta.prebind_only);
        // With no bindings, prebind_only has nothing to bind.
        let meta = route("'prebind_only': True");
        assert!(meta.rust_arg_bindings.is_none() && !meta.prebind_only);
    }

    // ---- RouteMetadata::from_python: auth backends and guards ----

    const JWT: &str = "{'type': 'jwt', 'secret': 'topsecret'}";

    #[test]
    fn auth_backends_are_parsed_in_order() {
        let meta = route(&format!(
            "'auth_backends': [{JWT}, {{'type': 'api_key', 'api_keys': ['k']}}, \
             {{'type': 'session', 'cookie': 'sid'}}]"
        ));
        assert_eq!(meta.auth_backends.len(), 3);
        assert!(matches!(meta.auth_backends[0], AuthBackend::JWT { .. }));
        assert!(matches!(meta.auth_backends[1], AuthBackend::APIKey { .. }));
        assert!(matches!(meta.auth_backends[2], AuthBackend::Session { .. }));
        assert_eq!(readings(meta.plan), only(7));
    }

    #[test]
    fn auth_backends_of_none_are_no_backends() {
        let meta = route("'auth_backends': None");
        assert!(meta.auth_backends.is_empty());
        assert!(!meta.plan.has_auth_or_guards());
        let meta = route("'auth_backends': []");
        assert!(!meta.plan.has_auth_or_guards());
    }

    #[test]
    fn auth_backends_that_are_not_a_list_of_dicts_stop_startup() {
        for value in ["5", "'jwt'", "[1]", "[{1: 2}]"] {
            let text = error_of(parse_route(&format!("'auth_backends': {value}")));
            assert!(
                text.starts_with("ValueError: Invalid 'auth_backends' route metadata: "),
                "{value}: {text}"
            );
        }
    }

    #[test]
    fn a_bad_auth_backend_stops_startup_and_does_not_leave_the_route_open() {
        assert_eq!(
            error_of(parse_route("'auth_backends': [{'type': 'jwt'}]")),
            "ValueError: JWT auth backend missing 'secret'"
        );
        assert_eq!(
            error_of(parse_route(&format!(
                "'auth_backends': [{JWT}, {{'type': 'ldap'}}]"
            ))),
            "ValueError: Unknown auth backend type 'ldap'"
        );
    }

    #[test]
    fn guards_are_parsed_and_set_the_auth_plan_flag_when_they_can_reject() {
        let meta = route("'guards': [{'type': 'is_authenticated'}]");
        assert!(!meta.guards.is_empty() && meta.guards.is_enforcing());
        assert_eq!(readings(meta.plan), only(7));

        let meta = route("'guards': [{'type': 'allow_any'}]");
        assert!(!meta.guards.is_empty() && !meta.guards.is_enforcing());
        assert_eq!(readings(meta.plan), [false; 12]);

        let meta = route("'guards': None");
        assert!(meta.guards.is_empty());
        assert_eq!(readings(meta.plan), [false; 12]);
    }

    #[test]
    fn guards_that_are_not_a_list_of_dicts_stop_startup() {
        for value in ["5", "'allow_any'", "[1]"] {
            let text = error_of(parse_route(&format!("'guards': {value}")));
            assert!(
                text.starts_with("ValueError: Invalid 'guards' route metadata: "),
                "{value}: {text}"
            );
        }
        assert_eq!(
            error_of(parse_route("'guards': [{'type': 'nope'}]")),
            "ValueError: Unknown guard type 'nope'"
        );
    }

    // ---- RouteMetadata::from_python: middleware ----

    const ORIGIN_A: &str = "{'type': 'cors', 'origins': ['https://a.test'], 'credentials': True}";

    #[test]
    fn a_cors_middleware_gives_the_route_cors_config() {
        let meta = route(&format!("'middleware': [{ORIGIN_A}]"));
        let cors = meta.cors_config.unwrap();
        assert_eq!(cors.origins, ["https://a.test"]);
        assert!(cors.credentials);
    }

    #[test]
    fn a_route_without_cors_middleware_has_no_cors_config() {
        assert!(route("'middleware': []").cors_config.is_none());
        assert!(
            route("'middleware': [{'type': 'compression'}]")
                .cors_config
                .is_none()
        );
        // A middleware entry with a type that is not a str is skipped.
        assert!(
            route("'middleware': [{'type': 5, 'origins': ['x']}]")
                .cors_config
                .is_none()
        );
        assert!(
            route("'middleware': [{'origins': ['x']}]")
                .cors_config
                .is_none()
        );
    }

    #[test]
    fn the_last_cors_config_replaces_the_outer_one() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origins': ['https://a.test'], 'max_age': 10}, \
             {'type': 'cors', 'origins': ['https://b.test'], 'max_age': 20}]",
        );
        let cors = meta.cors_config.unwrap();
        assert_eq!(cors.origins, ["https://b.test"]);
        assert!(cors.origin_set.contains("https://b.test"));
        assert!(!cors.origin_set.contains("https://a.test"));
        assert_eq!(cors.max_age, 20);
    }

    #[test]
    fn an_inner_cors_config_with_no_origins_keeps_the_outer_allow_list() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origins': ['https://a.test'], 'origin_regexes': ['^http://x$']}, \
             {'type': 'cors', 'credentials': True, 'max_age': 20}]",
        );
        let cors = meta.cors_config.unwrap();
        // The allow list comes from the outer config.
        assert_eq!(cors.origins, ["https://a.test"]);
        assert!(cors.origin_set.contains("https://a.test"));
        assert_eq!(cors.origin_regexes, ["^http://x$"]);
        assert_eq!(cors.compiled_origin_regexes.len(), 1);
        assert!(!cors.allow_all_origins);
        // The other settings come from the inner config.
        assert!(cors.credentials);
        assert_eq!(cors.max_age, 20);
    }

    #[test]
    fn an_inner_cors_config_keeps_the_wildcard_of_the_outer_one() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origins': ['*']}, {'type': 'cors', 'credentials': True}]",
        );
        let cors = meta.cors_config.unwrap();
        assert!(cors.allow_all_origins);
        assert!(cors.origin_set.contains("*"));
    }

    #[test]
    fn an_inner_cors_config_with_only_patterns_does_not_take_the_outer_origins() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origins': ['*']}, \
             {'type': 'cors', 'origin_regexes': ['^http://x$']}]",
        );
        let cors = meta.cors_config.unwrap();
        assert!(cors.origins.is_empty() && cors.origin_set.is_empty());
        assert!(!cors.allow_all_origins);
        assert_eq!(cors.origin_regexes, ["^http://x$"]);
    }

    #[test]
    fn an_inner_cors_config_with_only_origins_does_not_take_the_outer_patterns() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origin_regexes': ['^http://x$']}, \
             {'type': 'cors', 'origins': ['https://b.test']}]",
        );
        let cors = meta.cors_config.unwrap();
        assert_eq!(cors.origins, ["https://b.test"]);
        assert!(cors.origin_regexes.is_empty() && cors.compiled_origin_regexes.is_empty());
    }

    #[test]
    fn the_allow_list_passes_through_a_chain_of_cors_configs() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'origins': ['https://a.test']}, \
             {'type': 'cors', 'origins': ['https://b.test']}, {'type': 'cors', 'max_age': 5}]",
        );
        let cors = meta.cors_config.unwrap();
        assert_eq!(cors.origins, ["https://b.test"]);
        assert_eq!(cors.max_age, 5);
    }

    #[test]
    fn an_outer_cors_config_with_no_origins_adds_none_to_the_inner_config() {
        let meta = route(
            "'middleware': [{'type': 'cors', 'max_age': 5}, \
             {'type': 'cors', 'credentials': True}]",
        );
        let cors = meta.cors_config.unwrap();
        assert!(cors.origins.is_empty() && cors.origin_regexes.is_empty());
        assert!(cors.credentials);
        assert_eq!(cors.max_age, 3600);
    }

    #[test]
    fn a_bad_cors_config_stops_startup() {
        assert_eq!(
            error_of(parse_route(
                "'middleware': [{'type': 'cors', 'origins': 5}]"
            )),
            "TypeError: cors origins must be a str or a collection of str"
        );
    }

    #[test]
    fn a_rate_limit_middleware_gives_the_route_rate_limit_config() {
        let meta =
            route("'middleware': [{'type': 'rate_limit', 'rps': 5, 'burst': 9, 'key': 'x-key'}]");
        let limit = meta.rate_limit_config.unwrap();
        assert_eq!((limit.rps, limit.burst), (5, 9));
        assert_eq!(limit.key, RateLimitKey::Header("x-key".to_string()));
        assert_eq!(readings(meta.plan), only(8));
    }

    #[test]
    fn the_first_rate_limit_wins() {
        let meta = route(
            "'middleware': [{'type': 'rate_limit', 'rps': 5}, {'type': 'rate_limit', 'rps': 50}]",
        );
        assert_eq!(meta.rate_limit_config.unwrap().rps, 5);
    }

    #[test]
    fn a_route_without_rate_limit_middleware_has_no_rate_limit() {
        let meta = route("'middleware': [{'type': 'cors'}]");
        assert!(meta.rate_limit_config.is_none());
        assert!(!meta.plan.has_rate_limit());
    }

    #[test]
    fn an_identity_rate_limit_key_needs_an_auth_backend() {
        let user = "'middleware': [{'type': 'rate_limit', 'rps': 5, 'key': 'user'}]";
        let api_key = "'middleware': [{'type': 'rate_limit', 'rps': 5, 'key': 'api_key'}]";
        assert_eq!(
            error_of(parse_route(user)),
            "ValueError: @rate_limit(key=\"user\") needs an auth backend on the route or the API; \
             without one there is no identity to count per. Use key=\"ip\" or a header name."
        );
        assert_eq!(
            error_of(parse_route(api_key)),
            "ValueError: @rate_limit(key=\"api_key\") needs an auth backend on the route or the API; \
             without one there is no identity to count per. Use key=\"ip\" or a header name."
        );
        // Guards are not a backend.
        let guarded = format!("{user}, 'guards': [{{'type': 'is_authenticated'}}]");
        assert!(parse_route(&guarded).is_err());

        let backend = format!("'auth_backends': [{JWT}]");
        for key in [user, api_key] {
            let meta = route(&format!("{key}, {backend}"));
            assert!(meta.rate_limit_config.is_some());
            assert_eq!(readings(meta.plan), {
                let mut flags = only(7);
                flags[8] = true;
                flags
            });
        }
    }

    #[test]
    fn an_address_or_header_rate_limit_key_needs_no_auth_backend() {
        for key in ["ip", "X-Tenant"] {
            let meta = route(&format!(
                "'middleware': [{{'type': 'rate_limit', 'rps': 5, 'key': '{key}'}}]"
            ));
            assert!(meta.rate_limit_config.is_some());
            assert!(meta.auth_backends.is_empty());
        }
    }

    // ---- parse_auth_backend ----

    fn backend(code: &str) -> PyResult<AuthBackend> {
        with_py(|py| parse_auth_backend(&map(py, code), py))
    }

    /// The fields of a JWT backend, so a test can name only the ones it reads.
    struct Jwt {
        keys: JwtKeySource,
        algorithms: Vec<Algorithm>,
        header: String,
        cookie: Option<String>,
        audience: Option<String>,
        issuer: Option<String>,
        leeway: i64,
        token_type: Option<String>,
        require_jti: bool,
        cookie_csrf: bool,
    }

    fn jwt(code: &str) -> Jwt {
        match backend(code).unwrap() {
            AuthBackend::JWT {
                keys,
                algorithms,
                header,
                cookie,
                audience,
                issuer,
                leeway,
                token_type,
                require_jti,
                cookie_csrf,
            } => Jwt {
                keys,
                algorithms,
                header,
                cookie,
                audience,
                issuer,
                leeway,
                token_type,
                require_jti,
                cookie_csrf,
            },
            other => panic!("not a JWT backend: {other:?}"),
        }
    }

    const KID_JWKS: &str = r#"{"keys":[{"kty":"oct","kid":"k1","k":"c2VjcmV0"},{"kty":"oct","kid":"k2","k":"c2VjcmV0"}]}"#;

    #[test]
    fn a_jwt_backend_with_only_a_secret_gets_the_defaults() {
        let jwt = jwt("{'type': 'jwt', 'secret': 'topsecret'}");
        assert!(matches!(jwt.keys, JwtKeySource::Static(_)));
        assert_eq!(jwt.algorithms, [Algorithm::HS256]);
        assert_eq!(jwt.header, "authorization");
        assert_eq!((jwt.cookie, jwt.audience, jwt.issuer), (None, None, None));
        assert_eq!(jwt.leeway, 60);
        assert_eq!(jwt.token_type, None);
        assert!(!jwt.require_jti);
        assert!(jwt.cookie_csrf);
    }

    #[test]
    fn a_jwt_backend_with_none_for_each_optional_field_gets_the_defaults() {
        // `JWTAuthentication.to_metadata` sets these keys to None when unset.
        let jwt = jwt(
            "{'type': 'jwt', 'secret': 's', 'algorithms': None, 'header': None, \
             'cookie': None, 'audience': None, 'issuer': None, 'leeway': None, \
             'token_type': None, 'require_jti': None, 'cookie_csrf': None, 'jwks': None}",
        );
        assert!(matches!(jwt.keys, JwtKeySource::Static(_)));
        assert_eq!(jwt.algorithms, [Algorithm::HS256]);
        assert_eq!(jwt.header, "authorization");
        assert_eq!(
            (jwt.cookie, jwt.audience, jwt.issuer, jwt.token_type),
            (None, None, None, None)
        );
        assert_eq!(jwt.leeway, 60);
        assert!(!jwt.require_jti);
        assert!(jwt.cookie_csrf);
    }

    #[test]
    fn a_jwt_backend_takes_every_field_of_the_dict() {
        let jwt = jwt(
            "{'type': 'jwt', 'secret': 's', 'algorithms': ['HS384', 'HS512'], \
             'header': 'x-token', 'cookie': 'access', 'audience': 'api', 'issuer': 'https://idp.test', \
             'leeway': 5, 'token_type': 'refresh', 'require_jti': True, 'cookie_csrf': False}",
        );
        assert_eq!(jwt.algorithms, [Algorithm::HS384, Algorithm::HS512]);
        assert_eq!(jwt.header, "x-token");
        assert_eq!(jwt.cookie.as_deref(), Some("access"));
        assert_eq!(jwt.audience.as_deref(), Some("api"));
        assert_eq!(jwt.issuer.as_deref(), Some("https://idp.test"));
        assert_eq!(jwt.leeway, 5);
        assert_eq!(jwt.token_type.as_deref(), Some("refresh"));
        assert!(jwt.require_jti);
        assert!(!jwt.cookie_csrf);
        let negative = self::jwt("{'type': 'jwt', 'secret': 's', 'leeway': -3}");
        assert_eq!(negative.leeway, -3);
    }

    fn bearer(secret: &str) -> AHashMap<String, String> {
        bearer_with(secret, r#"{"alg":"HS256"}"#)
    }

    /// A token for `kid` signed with the secret of the test JWKS.
    fn bearer_kid(kid: &str) -> AHashMap<String, String> {
        bearer_with("secret", &format!(r#"{{"alg":"HS256","kid":"{kid}"}}"#))
    }

    fn bearer_with(secret: &str, header_json: &str) -> AHashMap<String, String> {
        let claims = format!(r#"{{"sub":"42","exp":{}}}"#, get_current_timestamp() + 3600);
        let message = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(header_json),
            URL_SAFE_NO_PAD.encode(claims)
        );
        let signature = crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_secret(secret.as_bytes()),
            Algorithm::HS256,
        )
        .unwrap();
        let mut headers = AHashMap::new();
        headers.insert(
            "authorization".to_string(),
            format!("Bearer {message}.{signature}"),
        );
        headers
    }

    #[test]
    fn a_jwt_backend_verifies_with_the_secret_of_the_dict() {
        let backends = [backend("{'type': 'jwt', 'secret': 'topsecret'}").unwrap()];
        assert!(authenticate(&bearer("topsecret"), &backends).is_some());
        assert!(authenticate(&bearer("other-secret"), &backends).is_none());
    }

    #[test]
    fn a_jwt_backend_with_a_bad_algorithm_list_stops_startup() {
        let text = error_of(backend(
            "{'type': 'jwt', 'secret': 's', 'algorithms': ['HS999']}",
        ));
        assert!(
            text.starts_with("ValueError: Unsupported JWT algorithm 'HS999'"),
            "{text}"
        );
        let text = error_of(backend("{'type': 'jwt', 'secret': 's', 'algorithms': []}"));
        assert_eq!(
            text,
            "ValueError: JWT auth backend requires at least one algorithm"
        );
        let text = error_of(backend(
            "{'type': 'jwt', 'secret': 's', 'algorithms': ['HS256', 'RS256']}",
        ));
        assert!(text.starts_with("ValueError: JWT algorithms "), "{text}");
        assert!(backend("{'type': 'jwt', 'secret': 's', 'algorithms': 'HS256'}").is_err());
    }

    #[test]
    fn a_jwt_backend_needs_a_str_secret_when_it_has_no_jwks() {
        assert_eq!(
            error_of(backend("{'type': 'jwt'}")),
            "ValueError: JWT auth backend missing 'secret'"
        );
        assert!(error_of(backend("{'type': 'jwt', 'secret': 5}")).starts_with("TypeError"));
        assert!(error_of(backend("{'type': 'jwt', 'secret': None}")).starts_with("TypeError"));
    }

    #[test]
    fn a_jwt_field_of_the_wrong_type_stops_startup_and_does_not_drop_the_backend() {
        for field in [
            "'header': 5",
            "'cookie': 5",
            "'audience': 5",
            "'issuer': 5",
            "'leeway': 'x'",
            "'token_type': 5",
            "'require_jti': 'yes'",
            "'cookie_csrf': 'yes'",
            "'jwks': 5",
        ] {
            let text = error_of(backend(&format!(
                "{{'type': 'jwt', 'secret': 's', {field}}}"
            )));
            assert!(text.starts_with("TypeError"), "{field}: {text}");
        }
    }

    #[test]
    fn a_jwks_document_gives_keys_by_kid_and_wins_over_the_secret() {
        for secret in ["", "'secret': 's', "] {
            let jwt = jwt(&format!(
                "{{'type': 'jwt', {secret}'algorithms': ['HS256'], 'jwks': '{KID_JWKS}'}}"
            ));
            let JwtKeySource::Jwks(keys) = jwt.keys else {
                panic!("expected a JWKS key source");
            };
            assert_eq!(keys.len(), 2);
            assert!(keys.contains_key("k1") && keys.contains_key("k2"));
        }
    }

    #[test]
    fn a_bad_jwks_document_stops_startup() {
        let text = error_of(backend(
            "{'type': 'jwt', 'secret': 's', 'jwks': 'not json'}",
        ));
        assert!(
            text.starts_with("ValueError: Invalid JWKS document for JWT auth"),
            "{text}"
        );
    }

    /// A JWT backend with a JWKS and a refresh callback that records its calls.
    /// `extra` is more dict items. Returns the backend and the list of calls.
    fn refreshing_backend<'py>(py: Python<'py>, extra: &str) -> (AuthBackend, Bound<'py, PyAny>) {
        let calls = eval(py, "[]", &[]);
        let refresh = eval(py, "lambda: calls.append(1)", &[("calls", calls.clone())]);
        let dict = eval(
            py,
            &format!("{{'type': 'jwt', 'jwks': '{KID_JWKS}', 'jwks_refresh': refresh, {extra}}}"),
            &[("refresh", refresh)],
        )
        .extract()
        .unwrap();
        (parse_auth_backend(&dict, py).unwrap(), calls)
    }

    fn has_refreshing_keys(backend: &AuthBackend) -> bool {
        matches!(
            backend,
            AuthBackend::JWT {
                keys: JwtKeySource::RefreshingJwks(_),
                ..
            }
        )
    }

    /// Wait up to `wait` for `calls` to hold a call.
    fn calls_within(py: Python<'_>, calls: &Bound<'_, PyAny>, wait: Duration) -> usize {
        let deadline = Instant::now() + wait;
        loop {
            let count = calls.len().unwrap();
            if count > 0 || Instant::now() >= deadline {
                return count;
            }
            py.detach(|| std::thread::sleep(Duration::from_millis(5)));
        }
    }

    #[test]
    fn a_jwks_refresh_callback_makes_a_refreshing_key_source_that_verifies_by_kid() {
        with_py(|py| {
            let (backend, calls) = refreshing_backend(py, "'jwks_refresh_interval': 3600");
            assert!(has_refreshing_keys(&backend));
            let backends = [backend];
            assert!(authenticate(&bearer_kid("k1"), &backends).is_some());
            assert!(authenticate(&bearer_kid("k2"), &backends).is_some());
            // The interval is not over and both kids are known: no fetch.
            assert_eq!(calls_within(py, &calls, Duration::from_millis(200)), 0);
        });
    }

    #[test]
    fn the_jwks_refresh_interval_defaults_to_300_seconds() {
        with_py(|py| {
            for interval in ["'x': 0", "'jwks_refresh_interval': None"] {
                let (backend, calls) = refreshing_backend(py, interval);
                let backends = [backend];
                assert!(authenticate(&bearer_kid("k1"), &backends).is_some());
                // The 300 s default is far off, so no fetch starts.
                assert_eq!(
                    calls_within(py, &calls, Duration::from_millis(300)),
                    0,
                    "{interval}"
                );
            }
        });
    }

    #[test]
    fn the_jwks_refresh_interval_of_the_dict_sets_the_fetch_time() {
        with_py(|py| {
            let (backend, calls) = refreshing_backend(py, "'jwks_refresh_interval': 0");
            let backends = [backend];
            assert!(authenticate(&bearer_kid("k1"), &backends).is_some());
            assert_eq!(calls_within(py, &calls, Duration::from_secs(5)), 1);
        });
    }

    #[test]
    fn a_jwks_refresh_interval_of_the_wrong_type_stops_startup() {
        with_py(|py| {
            let refresh = eval(py, "lambda: None", &[]);
            let dict = eval(
                py,
                &format!("{{'type': 'jwt', 'jwks': '{KID_JWKS}', 'jwks_refresh': refresh, 'jwks_refresh_interval': 'soon'}}"),
                &[("refresh", refresh)],
            )
            .extract()
            .unwrap();
            assert!(error_of(parse_auth_backend(&dict, py)).starts_with("TypeError"));
        });
    }

    #[test]
    fn a_none_jwks_refresh_callback_keeps_the_fixed_key_source() {
        let jwt = jwt(&format!(
            "{{'type': 'jwt', 'jwks': '{KID_JWKS}', 'jwks_refresh': None, 'jwks_refresh_interval': 5}}"
        ));
        assert!(matches!(jwt.keys, JwtKeySource::Jwks(_)));
    }

    #[test]
    fn an_api_key_backend_with_no_field_gets_the_defaults() {
        let AuthBackend::APIKey {
            api_keys,
            header,
            key_permissions,
        } = backend("{'type': 'api_key'}").unwrap()
        else {
            panic!("expected an API key backend");
        };
        assert!(api_keys.find("anything").is_none());
        assert_eq!(header, "x-api-key");
        assert!(key_permissions.is_empty());
        let AuthBackend::APIKey {
            api_keys,
            header,
            key_permissions,
        } = backend(
            "{'type': 'api_key', 'api_keys': None, 'header': None, 'key_permissions': None}",
        )
        .unwrap()
        else {
            panic!("expected an API key backend");
        };
        assert!(api_keys.find("anything").is_none());
        assert_eq!(header, "x-api-key");
        assert!(key_permissions.is_empty());
    }

    #[test]
    fn an_api_key_backend_takes_the_keys_header_and_permissions() {
        let AuthBackend::APIKey {
            api_keys,
            header,
            key_permissions,
        } = backend(
            "{'type': 'api_key', 'api_keys': ['k-one', 'k-two'], 'header': 'x-token', \
             'key_permissions': {'k-one': ['read', 'write'], 'k-two': []}}",
        )
        .unwrap()
        else {
            panic!("expected an API key backend");
        };
        assert_eq!(api_keys.find("k-one"), Some("k-one"));
        assert_eq!(api_keys.find("k-two"), Some("k-two"));
        assert!(api_keys.find("k-three").is_none());
        assert_eq!(header, "x-token");
        assert_eq!(key_permissions.len(), 2);
        assert_eq!(key_permissions["k-one"], ["read", "write"]);
        assert!(key_permissions["k-two"].is_empty());
    }

    #[test]
    fn an_api_key_field_of_the_wrong_type_stops_startup() {
        for field in [
            "'api_keys': 5",
            "'api_keys': [1]",
            "'header': 5",
            "'key_permissions': 5",
            "'key_permissions': {'k': 'read'}",
        ] {
            let text = error_of(backend(&format!("{{'type': 'api_key', {field}}}")));
            assert!(text.starts_with("TypeError"), "{field}: {text}");
        }
    }

    #[test]
    fn a_session_backend_needs_a_cookie_and_gets_csrf_on_and_no_redirect() {
        let AuthBackend::Session {
            cookie,
            csrf,
            login_redirect,
        } = backend("{'type': 'session', 'cookie': 'sessionid'}").unwrap()
        else {
            panic!("expected a session backend");
        };
        assert_eq!(cookie, "sessionid");
        assert!(csrf);
        assert!(!login_redirect);
    }

    #[test]
    fn a_session_backend_takes_the_csrf_and_redirect_flags() {
        let AuthBackend::Session {
            cookie,
            csrf,
            login_redirect,
        } = backend("{'type': 'session', 'cookie': 'sid', 'csrf': False, 'login_redirect': True}")
            .unwrap()
        else {
            panic!("expected a session backend");
        };
        assert_eq!(cookie, "sid");
        assert!(!csrf);
        assert!(login_redirect);
    }

    #[test]
    fn a_bad_session_backend_stops_startup() {
        assert_eq!(
            error_of(backend("{'type': 'session'}")),
            "ValueError: Session auth backend missing 'cookie'"
        );
        for field in [
            "'cookie': 5",
            "'cookie': 's', 'csrf': 'yes'",
            "'cookie': 's', 'login_redirect': 1",
        ] {
            let text = error_of(backend(&format!("{{'type': 'session', {field}}}")));
            assert!(text.starts_with("TypeError"), "{field}: {text}");
        }
    }

    #[test]
    fn an_auth_backend_needs_a_known_str_type() {
        assert_eq!(
            error_of(backend("{'secret': 's'}")),
            "ValueError: Auth backend metadata missing 'type'"
        );
        assert_eq!(
            error_of(backend("{'type': 'basic'}")),
            "ValueError: Unknown auth backend type 'basic'"
        );
        assert!(error_of(backend("{'type': 5}")).starts_with("TypeError"));
    }

    // ---- parse_guard and GuardCheck ----

    fn guard(code: &str) -> PyResult<Guard> {
        with_py(|py| parse_guard(&map(py, code), py))
    }

    /// The parts of a `Requires` guard.
    fn requires(code: &str) -> (ClaimKey, Vec<serde_json::Value>, Quantifier, Option<String>) {
        match guard(code).unwrap() {
            Guard::Requires {
                claim,
                values,
                quantifier,
                denial,
            } => (
                claim,
                values,
                quantifier,
                denial.map(|denial| denial.text.clone()),
            ),
            other => panic!("not a Requires guard: {other:?}"),
        }
    }

    #[test]
    fn the_simple_guards_have_no_fields() {
        assert!(matches!(
            guard("{'type': 'allow_any'}").unwrap(),
            Guard::AllowAny
        ));
        assert!(matches!(
            guard("{'type': 'is_authenticated'}").unwrap(),
            Guard::IsAuthenticated
        ));
    }

    #[test]
    fn a_guard_needs_a_known_str_type() {
        assert_eq!(
            error_of(guard("{}")),
            "ValueError: Guard metadata missing 'type'"
        );
        assert_eq!(
            error_of(guard("{'type': 'is_admin'}")),
            "ValueError: Unknown guard type 'is_admin'"
        );
        assert!(error_of(guard("{'type': 1}")).starts_with("TypeError"));
    }

    #[test]
    fn a_requires_guard_parses_the_claim_the_values_and_the_quantifier() {
        let (claim, values, quantifier, denial) = requires(
            "{'type': 'requires', 'claim': 'role', 'values': ['admin', 'staff'], 'quantifier': 1, 'message': None}",
        );
        assert_eq!(claim, ClaimKey::Extra("role".to_string()));
        assert_eq!(
            values,
            [serde_json::json!("admin"), serde_json::json!("staff")]
        );
        assert_eq!(quantifier, Quantifier::All);
        assert_eq!(denial, None);
    }

    #[test]
    fn a_requires_guard_names_a_typed_claim_with_its_own_key() {
        let (claim, ..) = requires(
            "{'type': 'requires', 'claim': 'is_staff', 'values': [True], 'quantifier': 0, 'message': None}",
        );
        assert_eq!(claim, ClaimKey::IsStaff);
        let (claim, ..) = requires(
            "{'type': 'requires', 'claim': 'permissions', 'values': [], 'quantifier': 0, 'message': None}",
        );
        assert_eq!(claim, ClaimKey::Permissions);
    }

    #[test]
    fn the_quantifier_numbers_are_any_all_and_none() {
        for (number, expected) in [
            (0, Quantifier::Any),
            (1, Quantifier::All),
            (2, Quantifier::None),
        ] {
            let (_, _, quantifier, _) = requires(&format!(
                "{{'type': 'requires', 'claim': 'c', 'values': ['v'], 'quantifier': {number}, 'message': None}}"
            ));
            assert_eq!(quantifier, expected, "{number}");
        }
        assert_eq!(
            error_of(guard(
                "{'type': 'requires', 'claim': 'c', 'values': [], 'quantifier': 3, 'message': None}"
            )),
            "ValueError: requires guard 'c' has unknown quantifier 3"
        );
    }

    #[test]
    fn a_requires_message_becomes_the_denial_with_a_json_body() {
        with_py(|py| {
            let dict = map(
                py,
                "{'type': 'requires', 'claim': 'c', 'values': [], 'quantifier': 0, 'message': 'Staff only'}",
            );
            let Guard::Requires { denial, .. } = parse_guard(&dict, py).unwrap() else {
                panic!("expected a Requires guard");
            };
            let denial = denial.unwrap();
            assert_eq!(denial.text, "Staff only");
            assert_eq!(&denial.body[..], br#"{"detail":"Staff only"}"#);
        });
    }

    #[test]
    fn a_requires_guard_with_a_missing_or_bad_field_stops_startup() {
        let full = [
            ("claim", "'c'"),
            ("values", "[]"),
            ("quantifier", "0"),
            ("message", "None"),
        ];
        for (missing, _) in full {
            let fields: Vec<String> = full
                .iter()
                .filter(|(key, _)| *key != missing)
                .map(|(key, value)| format!("'{key}': {value}"))
                .collect();
            assert_eq!(
                error_of(guard(&format!(
                    "{{'type': 'requires', {}}}",
                    fields.join(", ")
                ))),
                format!("ValueError: requires guard missing '{missing}'"),
                "{missing}"
            );
        }
        assert_eq!(
            error_of(guard(
                "{'type': 'requires', 'claim': '', 'values': [], 'quantifier': 0, 'message': None}"
            )),
            "ValueError: requires guard 'claim' must be a non-empty string"
        );
        for (key, bad) in [
            ("claim", "5"),
            ("values", "5"),
            ("quantifier", "'any'"),
            ("message", "5"),
        ] {
            let fields = full.map(|(k, v)| format!("'{k}': {}", if k == key { bad } else { v }));
            let text = error_of(guard(&format!(
                "{{'type': 'requires', {}}}",
                fields.join(", ")
            )));
            assert!(text.starts_with("TypeError"), "{key}: {text}");
        }
    }

    #[test]
    fn requires_values_keep_their_json_type() {
        let (_, values, ..) = requires(
            "{'type': 'requires', 'claim': 'c', 'quantifier': 0, 'message': None, \
             'values': [True, False, 1, 0, -7, 1.5, 2.0, 'text']}",
        );
        assert_eq!(
            values,
            [
                serde_json::json!(true),
                serde_json::json!(false),
                serde_json::json!(1),
                serde_json::json!(0),
                serde_json::json!(-7),
                serde_json::json!(1.5),
                serde_json::json!(2.0),
                serde_json::json!("text"),
            ]
        );
        // A bool is not a number: `True` must not become 1.
        assert!(values[0].is_boolean() && values[2].is_i64());
        assert!(values[6].is_f64());
    }

    #[test]
    fn a_requires_value_that_json_cannot_hold_stops_startup() {
        for value in ["float('nan')", "float('inf')", "float('-inf')"] {
            assert_eq!(
                error_of(guard(&format!(
                    "{{'type': 'requires', 'claim': 'c', 'quantifier': 0, 'message': None, 'values': [{value}]}}"
                ))),
                "ValueError: requires guard 'c' has a non-finite float value",
                "{value}"
            );
        }
        for value in ["None", "[1]", "b'x'", "{}"] {
            assert_eq!(
                error_of(guard(&format!(
                    "{{'type': 'requires', 'claim': 'c', 'quantifier': 0, 'message': None, 'values': [{value}]}}"
                ))),
                "ValueError: requires guard 'c' values must be str, int, float, or bool",
                "{value}"
            );
        }
    }

    fn guard_check(code: &str) -> GuardCheck {
        with_py(|py| GuardCheck::new(py, eval(py, code, &[]).extract().unwrap()).unwrap())
    }

    /// Run the check: None when it allows, else the status and the body.
    fn check(
        guards: &GuardCheck,
        user_id: Option<&str>,
        is_staff: bool,
        is_superuser: bool,
        permissions: &[&str],
    ) -> Option<(u16, Vec<u8>)> {
        with_py(|py| {
            guards
                .check(
                    py,
                    user_id.map(str::to_string),
                    is_staff,
                    is_superuser,
                    permissions.iter().map(|p| p.to_string()).collect(),
                )
                .map(|(status, body)| (status, body.as_bytes().to_vec()))
        })
    }

    #[test]
    fn a_guard_check_with_no_guards_allows_anyone() {
        let guards = guard_check("[]");
        assert_eq!(check(&guards, None, false, false, &[]), None);
        assert_eq!(check(&guards, Some("1"), false, false, &[]), None);
        let guards = guard_check("[{'type': 'allow_any'}]");
        assert_eq!(check(&guards, None, false, false, &[]), None);
    }

    #[test]
    fn a_guard_check_answers_401_to_an_anonymous_user() {
        let guards = guard_check("[{'type': 'is_authenticated'}]");
        assert_eq!(
            check(&guards, None, false, false, &[]),
            Some((401, ERROR_BODY_401.to_vec()))
        );
        assert_eq!(check(&guards, Some("7"), false, false, &[]), None);
    }

    #[test]
    fn a_guard_check_answers_403_with_the_default_or_the_custom_body() {
        let plain = guard_check(
            "[{'type': 'requires', 'claim': 'is_staff', 'values': [True], 'quantifier': 0, 'message': None}]",
        );
        assert_eq!(
            check(&plain, Some("7"), false, false, &[]),
            Some((403, ERROR_BODY_403.to_vec()))
        );
        assert_eq!(check(&plain, Some("7"), true, false, &[]), None);

        let custom = guard_check(
            "[{'type': 'requires', 'claim': 'is_staff', 'values': [True], 'quantifier': 0, 'message': 'Staff only'}]",
        );
        assert_eq!(
            check(&custom, Some("7"), false, false, &[]),
            Some((403, br#"{"detail":"Staff only"}"#.to_vec()))
        );
        // An anonymous user has no credential: 401, not 403.
        assert_eq!(
            check(&custom, None, true, true, &["x"]),
            Some((401, ERROR_BODY_401.to_vec()))
        );
    }

    #[test]
    fn a_guard_check_reads_the_staff_superuser_and_permission_state_of_the_user() {
        let staff = guard_check(
            "[{'type': 'requires', 'claim': 'is_staff', 'values': [], 'quantifier': 0, 'message': None}]",
        );
        assert_eq!(check(&staff, Some("1"), true, false, &[]), None);
        assert!(check(&staff, Some("1"), false, true, &[]).is_some());

        let superuser = guard_check(
            "[{'type': 'requires', 'claim': 'is_superuser', 'values': [], 'quantifier': 0, 'message': None}]",
        );
        assert_eq!(check(&superuser, Some("1"), false, true, &[]), None);
        assert!(check(&superuser, Some("1"), true, false, &[]).is_some());

        let permission = guard_check(
            "[{'type': 'requires', 'claim': 'permissions', 'values': ['app.edit'], 'quantifier': 0, 'message': None}]",
        );
        assert_eq!(
            check(
                &permission,
                Some("1"),
                false,
                false,
                &["app.edit", "app.view"]
            ),
            None
        );
        assert!(check(&permission, Some("1"), false, false, &["app.view"]).is_some());
        assert!(check(&permission, Some("1"), true, true, &[]).is_some());

        let subject = guard_check(
            "[{'type': 'requires', 'claim': 'sub', 'values': ['42'], 'quantifier': 0, 'message': None}]",
        );
        assert_eq!(check(&subject, Some("42"), false, false, &[]), None);
        assert!(check(&subject, Some("43"), false, false, &[]).is_some());
    }

    #[test]
    fn a_guard_check_runs_the_guards_in_order() {
        let guards = guard_check(
            "[{'type': 'is_authenticated'}, \
              {'type': 'requires', 'claim': 'is_staff', 'values': [True], 'quantifier': 0, 'message': 'no'}]",
        );
        assert_eq!(check(&guards, None, false, false, &[]).unwrap().0, 401);
        assert_eq!(check(&guards, Some("1"), false, false, &[]).unwrap().0, 403);
        assert_eq!(check(&guards, Some("1"), true, false, &[]), None);
    }

    #[test]
    fn a_bad_guard_stops_the_guard_check_build() {
        with_py(|py| {
            let guards = eval(py, "[{'type': 'is_authenticated'}, {'type': 'nope'}]", &[]);
            assert_eq!(
                error_of(GuardCheck::new(py, guards.extract().unwrap())),
                "ValueError: Unknown guard type 'nope'"
            );
        });
    }

    // ---- parse_file_constraints ----

    fn file_constraints(extra: &str) -> HashMap<String, FileFieldConstraints> {
        route(&format!("'file_constraints': {extra}")).file_constraints
    }

    #[test]
    fn file_constraints_read_each_limit_of_each_field() {
        let constraints = file_constraints(
            "{'avatar': {'max_size': 1000, 'min_size': 10, 'allowed_types': ['image/png', 'image/*'], 'max_files': 3}, \
              'doc': {'max_size': 5}}",
        );
        assert_eq!(constraints.len(), 2);
        let avatar = &constraints["avatar"];
        assert_eq!(avatar.max_size, Some(1000));
        assert_eq!(avatar.min_size, Some(10));
        assert_eq!(
            avatar.allowed_types.as_deref(),
            Some(&strings(&["image/png", "image/*"])[..])
        );
        assert_eq!(avatar.max_files, Some(3));
        let doc = &constraints["doc"];
        assert_eq!(doc.max_size, Some(5));
        assert!(doc.min_size.is_none() && doc.allowed_types.is_none() && doc.max_files.is_none());
    }

    #[test]
    fn a_field_with_no_limit_is_kept_with_no_limit() {
        let constraints = file_constraints("{'avatar': {}}");
        let avatar = &constraints["avatar"];
        assert!(avatar.max_size.is_none() && avatar.min_size.is_none());
        assert!(avatar.allowed_types.is_none() && avatar.max_files.is_none());
    }

    #[test]
    fn a_zero_file_limit_is_a_limit() {
        let avatar = &file_constraints(
            "{'avatar': {'max_size': 0, 'min_size': 0, 'max_files': 0}}",
        )["avatar"];
        assert_eq!(
            (avatar.max_size, avatar.min_size, avatar.max_files),
            (Some(0), Some(0), Some(0))
        );
    }

    #[test]
    fn a_file_limit_of_the_wrong_type_is_left_unset() {
        let avatar = &file_constraints(
            "{'avatar': {'max_size': 'big', 'min_size': -1, 'allowed_types': 'image/*', 'max_files': None}}",
        )["avatar"];
        assert!(avatar.max_size.is_none() && avatar.min_size.is_none());
        assert!(avatar.allowed_types.is_none() && avatar.max_files.is_none());
    }

    #[test]
    fn file_constraints_that_are_not_a_map_of_maps_are_ignored() {
        assert!(file_constraints("5").is_empty());
        assert!(file_constraints("{'avatar': 5}").is_empty());
        assert!(file_constraints("None").is_empty());
        assert!(route("").file_constraints.is_empty());
    }

    // ---- parse_rust_arg_bindings ----

    fn bindings(code: &str) -> Option<Vec<RustArgBinding>> {
        with_py(|py| {
            parse_rust_arg_bindings(&py_dict(py, &format!("{{'rust_arg_bindings': {code}}}")))
        })
    }

    fn entry(source: &str, key: &str, name: &str, kind: &str, rest: &str) -> String {
        format!(
            "{{'source': {source}, 'lookup_key': {key}, 'arg_name': {name}, 'arg_kind': {kind}{rest}}}"
        )
    }

    fn one(source: &str, key: &str, name: &str, kind: &str, rest: &str) -> Option<RustArgBinding> {
        let list = format!("[{}]", entry(source, key, name, kind, rest));
        bindings(&list).map(|mut found| {
            assert_eq!(found.len(), 1);
            found.remove(0)
        })
    }

    #[test]
    fn no_binding_list_gives_no_plan() {
        with_py(|py| {
            assert!(parse_rust_arg_bindings(&py_dict(py, "{}")).is_none());
            assert!(parse_rust_arg_bindings(&py_dict(py, "{'rust_arg_bindings': None}")).is_none());
        });
        assert!(bindings("[]").is_none());
        assert!(bindings("5").is_none());
        assert!(bindings("{'source': 'query'}").is_none());
        assert!(bindings("(1,)").is_none());
    }

    #[test]
    fn a_binding_reads_the_source_the_keys_and_the_kind() {
        with_py(|py| {
            let found = one("'query'", "'page-no'", "'page_no'", "'keyword'", "").unwrap();
            assert_eq!(found.source, RustArgSource::Query);
            assert_eq!(found.key, "page-no");
            assert_eq!(found.arg_name.bind(py).to_str().unwrap(), "page_no");
            assert!(!found.positional);
            // The defaults of the older shape.
            assert!(found.required);
            assert!(!found.inject_none);
        });
    }

    #[test]
    fn each_source_name_gives_its_source() {
        for (name, source) in [
            ("path", RustArgSource::Path),
            ("query", RustArgSource::Query),
            ("header", RustArgSource::Header),
            ("cookie", RustArgSource::Cookie),
        ] {
            let found = one(&format!("'{name}'"), "'k'", "'a'", "'keyword'", "").unwrap();
            assert_eq!(found.source, source, "{name}");
        }
        for bad in ["'body'", "'Query'", "None", "5"] {
            assert!(one(bad, "'k'", "'a'", "'keyword'", "").is_none(), "{bad}");
        }
        assert!(
            bindings("[{'lookup_key': 'k', 'arg_name': 'a', 'arg_kind': 'keyword'}]").is_none()
        );
    }

    #[test]
    fn a_binding_needs_a_non_empty_lookup_key_and_arg_name() {
        for bad in ["''", "None", "5"] {
            assert!(
                one("'path'", bad, "'a'", "'keyword'", "").is_none(),
                "key {bad}"
            );
            assert!(
                one("'path'", "'k'", bad, "'keyword'", "").is_none(),
                "name {bad}"
            );
        }
        assert!(bindings("[{'source': 'path', 'arg_name': 'a', 'arg_kind': 'keyword'}]").is_none());
        assert!(
            bindings("[{'source': 'path', 'lookup_key': 'k', 'arg_kind': 'keyword'}]").is_none()
        );
    }

    #[test]
    fn the_arg_kind_is_positional_or_keyword() {
        let found = one("'path'", "'id'", "'id'", "'positional'", "").unwrap();
        assert!(found.positional);
        let found = one("'path'", "'id'", "'id'", "'keyword'", "").unwrap();
        assert!(!found.positional);
        for bad in ["'other'", "None", "5", "'Positional'"] {
            assert!(one("'path'", "'id'", "'id'", bad, "").is_none(), "{bad}");
        }
        assert!(bindings("[{'source': 'path', 'lookup_key': 'k', 'arg_name': 'a'}]").is_none());
    }

    #[test]
    fn a_binding_takes_the_required_and_inject_none_flags() {
        let found = one(
            "'query'",
            "'q'",
            "'q'",
            "'keyword'",
            ", 'required': False, 'inject_none': True",
        )
        .unwrap();
        assert!(!found.required && found.inject_none);
        let found = one(
            "'query'",
            "'q'",
            "'q'",
            "'keyword'",
            ", 'required': True, 'inject_none': False",
        )
        .unwrap();
        assert!(found.required && !found.inject_none);
        // A flag of another type keeps the default.
        let found = one(
            "'query'",
            "'q'",
            "'q'",
            "'keyword'",
            ", 'required': 'no', 'inject_none': 'yes'",
        )
        .unwrap();
        assert!(found.required && !found.inject_none);
    }

    #[test]
    fn an_optional_positional_binding_voids_the_plan() {
        // A skipped positional slot would shift the next positional argument.
        assert!(
            one(
                "'path'",
                "'a'",
                "'a'",
                "'positional'",
                ", 'required': False"
            )
            .is_none()
        );
        assert!(one("'path'", "'a'", "'a'", "'keyword'", ", 'required': False").is_some());
        assert!(one("'path'", "'a'", "'a'", "'positional'", ", 'required': True").is_some());
    }

    #[test]
    fn bindings_keep_their_order_and_one_bad_entry_voids_the_plan() {
        let good = |name: &str| {
            entry(
                "'header'",
                &format!("'x-{name}'"),
                &format!("'{name}'"),
                "'keyword'",
                "",
            )
        };
        let found = bindings(&format!("[{}, {}, {}]", good("a"), good("b"), good("c"))).unwrap();
        let keys: Vec<&str> = found.iter().map(|binding| binding.key.as_str()).collect();
        assert_eq!(keys, ["x-a", "x-b", "x-c"]);

        let bad = entry("'nowhere'", "'k'", "'a'", "'keyword'", "");
        assert!(bindings(&format!("[{}, {bad}]", good("a"))).is_none());
        assert!(bindings(&format!("[{bad}, {}]", good("a"))).is_none());
        assert!(bindings(&format!("[{}, 5]", good("a"))).is_none());
    }

    #[test]
    fn a_cloned_binding_has_the_same_fields() {
        with_py(|py| {
            let found = one(
                "'cookie'",
                "'sid'",
                "'session'",
                "'keyword'",
                ", 'required': False, 'inject_none': True",
            )
            .unwrap();
            let copy = found.clone();
            assert_eq!(copy.source, RustArgSource::Cookie);
            assert_eq!(copy.key, "sid");
            assert!(copy.arg_name.bind(py).is(found.arg_name.bind(py)));
            assert!(!copy.positional && !copy.required && copy.inject_none);
            let positional = one("'path'", "'a'", "'a'", "'positional'", "")
                .unwrap()
                .clone();
            assert!(positional.positional && positional.required && !positional.inject_none);
        });
    }

    // ---- CompressionConfig::from_python_dict ----

    fn compression(code: &str) -> PyResult<CompressionConfig> {
        with_py(|py| CompressionConfig::from_python_dict(&eval(py, code, &[])))
    }

    const COMPRESSION: &str = "'backend': 'gzip', 'minimum_size': 256, 'gzip_fallback': False";

    #[test]
    fn compression_takes_the_required_fields_and_defaults_the_tuning_fields() {
        let config = compression(&format!("{{{COMPRESSION}}}")).unwrap();
        assert_eq!(config.backend, "gzip");
        assert_eq!(config.minimum_size, 256);
        assert!(!config.gzip_fallback);
        assert_eq!(
            (
                config.brotli_level,
                config.brotli_lgwin,
                config.gzip_level,
                config.zstd_level
            ),
            (5, 14, 6, 3)
        );
        let config =
            compression("{'backend': 'zstd', 'minimum_size': 0, 'gzip_fallback': True}").unwrap();
        assert_eq!(
            (
                config.backend.as_str(),
                config.minimum_size,
                config.gzip_fallback
            ),
            ("zstd", 0, true)
        );
    }

    #[test]
    fn compression_takes_each_tuning_field_into_its_own_level() {
        let config = compression(&format!(
            "{{{COMPRESSION}, 'brotli_level': 1, 'brotli_lgwin': 20, 'gzip_level': 8, 'zstd_level': 19}}"
        ))
        .unwrap();
        assert_eq!(
            (
                config.brotli_level,
                config.brotli_lgwin,
                config.gzip_level,
                config.zstd_level
            ),
            (1, 20, 8, 19)
        );
        // None is the same as a missing field.
        let config = compression(&format!(
            "{{{COMPRESSION}, 'brotli_level': None, 'brotli_lgwin': None, 'gzip_level': None, 'zstd_level': None}}"
        ))
        .unwrap();
        assert_eq!(
            (
                config.brotli_level,
                config.brotli_lgwin,
                config.gzip_level,
                config.zstd_level
            ),
            (5, 14, 6, 3)
        );
    }

    #[test]
    fn a_tuning_field_accepts_both_ends_of_its_range_and_no_value_beyond() {
        for (key, lo, hi) in [
            ("brotli_level", 0, 11),
            ("brotli_lgwin", 10, 24),
            ("gzip_level", 0, 9),
            ("zstd_level", 1, 22),
        ] {
            for ok in [lo, hi] {
                let config = compression(&format!("{{{COMPRESSION}, '{key}': {ok}}}")).unwrap();
                let level = match key {
                    "brotli_level" => config.brotli_level,
                    "brotli_lgwin" => config.brotli_lgwin,
                    "gzip_level" => config.gzip_level,
                    _ => config.zstd_level,
                };
                assert_eq!(level, ok, "{key}");
            }
            for bad in [lo as i64 - 1, hi as i64 + 1] {
                assert_eq!(
                    error_of(compression(&format!("{{{COMPRESSION}, '{key}': {bad}}}"))),
                    if bad < 0 {
                        format!(
                            "ValueError: CompressionConfig: '{key}' must be an int in [{lo}, {hi}]"
                        )
                    } else {
                        format!(
                            "ValueError: CompressionConfig: '{key}'={bad} out of range [{lo}, {hi}]"
                        )
                    },
                    "{key} = {bad}"
                );
            }
        }
    }

    #[test]
    fn a_tuning_field_of_the_wrong_type_stops_startup() {
        assert_eq!(
            error_of(compression(&format!(
                "{{{COMPRESSION}, 'brotli_level': 'high'}}"
            ))),
            "ValueError: CompressionConfig: 'brotli_level' must be an int in [0, 11]"
        );
        assert_eq!(
            error_of(compression(&format!(
                "{{{COMPRESSION}, 'zstd_level': 1.5}}"
            ))),
            "ValueError: CompressionConfig: 'zstd_level' must be an int in [1, 22]"
        );
    }

    #[test]
    fn a_none_required_compression_field_is_a_value_error_that_names_it() {
        for key in ["backend", "minimum_size", "gzip_fallback"] {
            let fields: Vec<String> = [
                ("backend", "'gzip'"),
                ("minimum_size", "256"),
                ("gzip_fallback", "True"),
            ]
            .iter()
            .map(|(k, v)| format!("'{k}': {}", if *k == key { "None" } else { v }))
            .collect();
            assert_eq!(
                error_of(compression(&format!("{{{}}}", fields.join(", ")))),
                format!("ValueError: CompressionConfig: required field '{key}' is missing"),
                "{key}"
            );
        }
    }

    #[test]
    fn a_required_compression_field_of_the_wrong_type_is_a_value_error() {
        assert_eq!(
            error_of(compression(
                "{'backend': 5, 'minimum_size': 1, 'gzip_fallback': True}"
            )),
            "ValueError: CompressionConfig: 'backend' must be a string"
        );
        assert_eq!(
            error_of(compression(
                "{'backend': 'gzip', 'minimum_size': -1, 'gzip_fallback': True}"
            )),
            "ValueError: CompressionConfig: 'minimum_size' must be a non-negative int"
        );
        assert_eq!(
            error_of(compression(
                "{'backend': 'gzip', 'minimum_size': 'x', 'gzip_fallback': True}"
            )),
            "ValueError: CompressionConfig: 'minimum_size' must be a non-negative int"
        );
        assert_eq!(
            error_of(compression(
                "{'backend': 'gzip', 'minimum_size': 1, 'gzip_fallback': 'yes'}"
            )),
            "ValueError: CompressionConfig: 'gzip_fallback' must be a bool"
        );
    }

    // ---- RouteMetadataStore ----

    #[test]
    fn a_metadata_store_gives_each_route_by_its_handler_id() {
        let store = RouteMetadataStore::from_map(
            [
                (2usize, route("'default_status_code': 202")),
                (5, route("'default_status_code': 205")),
            ]
            .into_iter()
            .collect(),
        );
        assert_eq!(store.get(2).unwrap().default_status_code, 202);
        assert_eq!(store.get(5).unwrap().default_status_code, 205);
        for missing in [0, 1, 3, 4, 6, 100] {
            assert!(store.get(missing).is_none(), "{missing}");
        }
    }

    #[test]
    fn a_metadata_store_with_one_route_at_id_zero_finds_it() {
        let store = RouteMetadataStore::from_map(
            [(0usize, route("'default_status_code': 204"))]
                .into_iter()
                .collect(),
        );
        assert_eq!(store.get(0).unwrap().default_status_code, 204);
        assert!(store.get(1).is_none());
    }

    #[test]
    fn an_empty_metadata_store_has_no_route() {
        let store = RouteMetadataStore::from_map(AHashMap::new());
        assert!(store.get(0).is_none());
        assert!(RouteMetadataStore::default().get(0).is_none());
    }
}
