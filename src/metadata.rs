/// Route metadata parsing from Python to Rust types
///
/// This module handles parsing Python metadata dicts into strongly-typed
/// Rust enums at registration time, eliminating per-request GIL overhead.
use actix_web::http::header::HeaderValue;
use ahash::{AHashMap, AHashSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use regex::Regex;
use std::collections::{HashMap, HashSet};

use crate::form_parsing::FileFieldConstraints;
use crate::middleware::auth::AuthBackend;
use crate::permissions::Guard;

/// Request value source for Rust-side argument prebinding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RustArgSource {
    Path,
    Query,
    Header,
    Cookie,
}

/// One argument binding entry used by Rust-side prebinding.
#[derive(Debug, Clone)]
pub struct RustArgBinding {
    pub source: RustArgSource,
    pub lookup_key: String,
    pub arg_name: String,
    pub positional: bool,
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
        let mut config = CorsConfig::default();

        // Build origin set for O(1) lookups
        config.origin_set = origins.iter().cloned().collect();
        config.origins = origins;

        // Compile origin regex patterns at startup
        config.origin_regexes = origin_regexes.clone();
        config.compiled_origin_regexes = origin_regexes
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

        config.allow_all_origins = allow_all_origins;
        config.credentials = allow_credentials;

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
    pub key_type: String,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        RateLimitConfig {
            rps: 100,
            burst: 200,
            key_type: "ip".to_string(),
        }
    }
}

/// Compression configuration parsed at startup
#[derive(Debug, Clone)]
pub struct CompressionConfig {
    pub backend: String,     // "gzip", "brotli", "zstd"
    pub minimum_size: usize, // Minimum response size to compress (bytes)
    pub gzip_fallback: bool, // Fall back to gzip if backend not supported
}

impl Default for CompressionConfig {
    fn default() -> Self {
        CompressionConfig {
            backend: "brotli".to_string(),
            minimum_size: 500,
            gzip_fallback: true,
        }
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
        let mut by_handler_id = vec![None; max_id + 1];
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
    pub guards: Vec<Guard>,
    pub skip: HashSet<String>,
    pub cors_config: Option<CorsConfig>,
    pub rate_limit_config: Option<RateLimitConfig>,

    #[allow(dead_code)] // Reserved for future optimization
    pub needs_path_params: bool,
    #[allow(dead_code)] // Reserved for future optimization
    pub is_static_route: bool,

    // Type hints for path/query parameters (enables Rust-side type coercion)
    // Maps parameter name to type hint constant (see type_coercion.rs)
    pub param_types: HashMap<String, u8>,

    // Form-related metadata for Rust-side form parsing
    pub form_type_hints: HashMap<String, u8>,
    pub file_constraints: HashMap<String, FileFieldConstraints>,
    pub max_upload_size: usize,
    pub memory_spool_threshold: usize,
    pub rust_arg_bindings: Option<Vec<RustArgBinding>>,
    pub plan: RouteExecutionPlan,
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

        if let Ok(Some(mw_list)) = py_meta.get_item("middleware") {
            if let Ok(py_list) = mw_list.extract::<Vec<HashMap<String, Py<PyAny>>>>() {
                for mw_dict in py_list {
                    if let Some(mw_type) = mw_dict.get("type") {
                        if let Ok(type_str) = mw_type.extract::<String>(py) {
                            // Parse CORS config separately for fast access
                            if type_str == "cors" && cors_config.is_none() {
                                cors_config = parse_cors_config(&mw_dict, py);
                            }

                            // Parse rate_limit config separately for fast access
                            if type_str == "rate_limit" && rate_limit_config.is_none() {
                                rate_limit_config = parse_rate_limit_config(&mw_dict, py);
                            }
                        }
                    }
                }
            }
        }

        // Parse auth backends
        if let Ok(Some(auth_list)) = py_meta.get_item("auth_backends") {
            if let Ok(py_backends) = auth_list.extract::<Vec<HashMap<String, Py<PyAny>>>>() {
                for backend_dict in py_backends {
                    if let Some(backend) = parse_auth_backend(&backend_dict, py) {
                        auth_backends.push(backend);
                    }
                }
            }
        }

        // Parse guards
        if let Ok(Some(guard_list)) = py_meta.get_item("guards") {
            if let Ok(py_guards) = guard_list.extract::<Vec<HashMap<String, Py<PyAny>>>>() {
                for guard_dict in py_guards {
                    if let Some(guard) = parse_guard(&guard_dict, py) {
                        guards.push(guard);
                    }
                }
            }
        }

        // Parse skip list (e.g., ["compression", "cors"]) into a set
        if let Ok(Some(skip_list)) = py_meta.get_item("skip") {
            if let Ok(names) = skip_list.extract::<Vec<String>>() {
                for name in names {
                    skip.insert(name.to_lowercase());
                }
            }
        }

        // Parse optimization flags (default to true for backward compatibility)
        // These flags indicate which request components the handler actually needs
        let needs_body = py_meta
            .get_item("needs_body")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let needs_query = py_meta
            .get_item("needs_query")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let needs_headers = py_meta
            .get_item("needs_headers")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let needs_cookies = py_meta
            .get_item("needs_cookies")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let needs_path_params = py_meta
            .get_item("needs_path_params")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(true);

        let is_static_route = py_meta
            .get_item("is_static_route")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);

        // Parse param_types for Rust-side type coercion
        // Format: {"param_name": type_hint_id, ...}
        // Type hint IDs match type_coercion.rs constants (TYPE_INT=1, TYPE_FLOAT=2, etc.)
        let param_types: HashMap<String, u8> = py_meta
            .get_item("param_types")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<HashMap<String, u8>>().ok())
            .unwrap_or_default();

        // Parse form-related metadata for Rust-side form parsing
        let needs_form_parsing = py_meta
            .get_item("needs_form_parsing")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(false);

        let skip_cors = skip.contains("cors");
        let skip_compression = skip.contains("compression");
        let has_auth_or_guards = !auth_backends.is_empty() || !guards.is_empty();
        let has_rate_limit = rate_limit_config.is_some();
        let can_sync_dispatch = py_meta
            .get_item("can_sync_dispatch")
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
        );

        // Form field type hints (same format as param_types)
        let form_type_hints: HashMap<String, u8> = py_meta
            .get_item("form_type_hints")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<HashMap<String, u8>>().ok())
            .unwrap_or_default();

        // File field constraints
        let file_constraints = parse_file_constraints(py_meta, py);

        // Max upload size (default 1MB)
        let max_upload_size = py_meta
            .get_item("max_upload_size")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<usize>().ok())
            .unwrap_or(1024 * 1024);

        // Memory spool threshold - when to spool files to disk (default 1MB)
        let memory_spool_threshold = py_meta
            .get_item("memory_spool_threshold")
            .ok()
            .flatten()
            .and_then(|v| v.extract::<usize>().ok())
            .unwrap_or(1024 * 1024);

        // Optional Rust-side argument binding plan.
        let rust_arg_bindings = parse_rust_arg_bindings(py_meta);

        Ok(RouteMetadata {
            auth_backends,
            guards,
            skip,
            cors_config,
            rate_limit_config,
            needs_path_params,
            is_static_route,
            param_types,
            form_type_hints,
            file_constraints,
            max_upload_size,
            memory_spool_threshold,
            rust_arg_bindings,
            plan,
        })
    }
}

/// Parse CORS configuration from middleware dict
fn parse_cors_config(dict: &HashMap<String, Py<PyAny>>, py: Python) -> Option<CorsConfig> {
    let mut config = CorsConfig::default();

    // Parse origins and build origin set for O(1) lookups
    if let Some(origins_py) = dict.get("origins") {
        if let Ok(origins) = origins_py.extract::<Vec<String>>(py) {
            // Detect wildcard origin
            config.allow_all_origins = origins.iter().any(|o| o == "*");
            config.origin_set = origins.iter().cloned().collect();
            config.origins = origins;
        }
    }

    // Parse origin_regexes and compile them at startup
    if let Some(regexes_py) = dict.get("origin_regexes") {
        if let Ok(regex_patterns) = regexes_py.extract::<Vec<String>>(py) {
            config.origin_regexes = regex_patterns.clone();
            config.compiled_origin_regexes = regex_patterns.iter()
                .filter_map(|pattern| {
                    Regex::new(pattern).ok().or_else(|| {
                        eprintln!("[django-bolt] Warning: Invalid route-level CORS origin regex pattern: {}", pattern);
                        None
                    })
                })
                .collect();
        }
    }

    // Parse credentials
    if let Some(cred_py) = dict.get("credentials") {
        if let Ok(cred) = cred_py.extract::<bool>(py) {
            config.credentials = cred;
        }
    }

    // Parse methods (optional, has defaults)
    if let Some(methods_py) = dict.get("methods") {
        if let Ok(methods) = methods_py.extract::<Vec<String>>(py) {
            config.methods_str = methods.join(", ");
            config.methods_header = HeaderValue::from_str(&config.methods_str).ok();
            config.methods = methods;
        }
    }

    // Parse headers (optional, has defaults)
    if let Some(headers_py) = dict.get("headers") {
        if let Ok(headers) = headers_py.extract::<Vec<String>>(py) {
            config.headers_str = headers.join(", ");
            config.headers_header = HeaderValue::from_str(&config.headers_str).ok();
            config.headers = headers;
        }
    }

    // Parse expose_headers (optional)
    if let Some(expose_py) = dict.get("expose_headers") {
        if let Ok(expose) = expose_py.extract::<Vec<String>>(py) {
            config.expose_headers_str = expose.join(", ");
            config.expose_headers_header = if !expose.is_empty() {
                HeaderValue::from_str(&config.expose_headers_str).ok()
            } else {
                None
            };
            config.expose_headers = expose;
        }
    }

    // Parse max_age (optional)
    if let Some(age_py) = dict.get("max_age") {
        if let Ok(age) = age_py.extract::<u32>(py) {
            config.max_age_str = age.to_string();
            config.max_age_header = HeaderValue::from_str(&config.max_age_str).ok();
            config.max_age = age;
        }
    }

    Some(config)
}

/// Parse rate limiting configuration from middleware dict
fn parse_rate_limit_config(
    dict: &HashMap<String, Py<PyAny>>,
    py: Python,
) -> Option<RateLimitConfig> {
    let mut config = RateLimitConfig::default();

    // Parse rps (required)
    if let Some(rps_py) = dict.get("rps") {
        if let Ok(rps) = rps_py.extract::<u32>(py) {
            config.rps = rps;
        }
    }

    // Parse burst (optional, defaults to 2x rps)
    if let Some(burst_py) = dict.get("burst") {
        if let Ok(burst) = burst_py.extract::<u32>(py) {
            config.burst = burst;
        }
    } else {
        // If burst not specified, default to 2x rps
        config.burst = config.rps * 2;
    }

    // Parse key_type (optional, defaults to "ip")
    if let Some(key_py) = dict.get("key") {
        if let Ok(key_type) = key_py.extract::<String>(py) {
            config.key_type = key_type;
        }
    }

    Some(config)
}

/// Parse a single auth backend from Python dict
fn parse_auth_backend(dict: &HashMap<String, Py<PyAny>>, py: Python) -> Option<AuthBackend> {
    let backend_type = dict.get("type")?.extract::<String>(py).ok()?;

    match backend_type.as_str() {
        "jwt" => {
            let secret = dict.get("secret")?.extract::<String>(py).ok()?;
            let algorithms = dict
                .get("algorithms")
                .and_then(|a| a.extract::<Vec<String>>(py).ok())
                .unwrap_or_else(|| vec!["HS256".to_string()]);
            let header = dict
                .get("header")
                .and_then(|h| h.extract::<String>(py).ok())
                .unwrap_or_else(|| "authorization".to_string());
            let audience = dict
                .get("audience")
                .and_then(|a| a.extract::<String>(py).ok());
            let issuer = dict
                .get("issuer")
                .and_then(|i| i.extract::<String>(py).ok());

            Some(AuthBackend::JWT {
                secret,
                algorithms,
                header,
                audience,
                issuer,
            })
        }
        "api_key" => {
            let api_keys_list = dict
                .get("api_keys")
                .and_then(|k| k.extract::<Vec<String>>(py).ok())
                .unwrap_or_default();
            let api_keys: HashSet<String> = api_keys_list.into_iter().collect();

            let header = dict
                .get("header")
                .and_then(|h| h.extract::<String>(py).ok())
                .unwrap_or_else(|| "x-api-key".to_string());

            let key_permissions = dict
                .get("key_permissions")
                .and_then(|kp| kp.extract::<HashMap<String, Vec<String>>>(py).ok())
                .unwrap_or_default();

            Some(AuthBackend::APIKey {
                api_keys,
                header,
                key_permissions,
            })
        }
        _ => None,
    }
}

/// Parse a single guard from Python dict
fn parse_guard(dict: &HashMap<String, Py<PyAny>>, py: Python) -> Option<Guard> {
    let guard_type = dict.get("type")?.extract::<String>(py).ok()?;

    match guard_type.as_str() {
        "allow_any" => Some(Guard::AllowAny),
        "is_authenticated" => Some(Guard::IsAuthenticated),
        "is_superuser" => Some(Guard::IsSuperuser),
        "is_staff" => Some(Guard::IsStaff),
        "has_permission" => {
            let perm = dict.get("permission")?.extract::<String>(py).ok()?;
            Some(Guard::HasPermission(perm))
        }
        "has_any_permission" => {
            let perms = dict.get("permissions")?.extract::<Vec<String>>(py).ok()?;
            Some(Guard::HasAnyPermission(perms))
        }
        "has_all_permissions" => {
            let perms = dict.get("permissions")?.extract::<Vec<String>>(py).ok()?;
            Some(Guard::HasAllPermissions(perms))
        }
        _ => None,
    }
}

/// Parse file field constraints from Python metadata
/// Format: {"field_name": {"max_size": 1024, "min_size": 0, "allowed_types": ["image/*"], "max_files": 5}, ...}
fn parse_file_constraints(
    py_meta: &Bound<'_, PyDict>,
    py: Python,
) -> HashMap<String, FileFieldConstraints> {
    let mut result = HashMap::new();

    if let Ok(Some(constraints_dict)) = py_meta.get_item("file_constraints") {
        if let Ok(constraints_map) =
            constraints_dict.extract::<HashMap<String, HashMap<String, Py<PyAny>>>>()
        {
            for (field_name, field_constraints) in constraints_map {
                let mut constraints = FileFieldConstraints::default();

                if let Some(max_size_py) = field_constraints.get("max_size") {
                    if let Ok(max_size) = max_size_py.extract::<usize>(py) {
                        constraints.max_size = Some(max_size);
                    }
                }

                if let Some(min_size_py) = field_constraints.get("min_size") {
                    if let Ok(min_size) = min_size_py.extract::<usize>(py) {
                        constraints.min_size = Some(min_size);
                    }
                }

                if let Some(allowed_types_py) = field_constraints.get("allowed_types") {
                    if let Ok(allowed_types) = allowed_types_py.extract::<Vec<String>>(py) {
                        constraints.allowed_types = Some(allowed_types);
                    }
                }

                if let Some(max_files_py) = field_constraints.get("max_files") {
                    if let Ok(max_files) = max_files_py.extract::<usize>(py) {
                        constraints.max_files = Some(max_files);
                    }
                }

                result.insert(field_name, constraints);
            }
        }
    }

    result
}

/// Parse Rust-side argument binding plan from Python metadata.
///
/// Expected format:
/// [
///   {"source": "path|query|header|cookie", "lookup_key": "...", "arg_name": "...", "arg_kind": "positional|keyword"},
///   ...
/// ]
fn parse_rust_arg_bindings(py_meta: &Bound<'_, PyDict>) -> Option<Vec<RustArgBinding>> {
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

        bindings.push(RustArgBinding {
            source,
            lookup_key,
            arg_name,
            positional,
        });
    }

    if bindings.is_empty() {
        None
    } else {
        Some(bindings)
    }
}
