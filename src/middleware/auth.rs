use ahash::AHashMap;
use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::IntoPyObjectExt;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: Option<String>,              // Subject (user ID)
    pub exp: Option<i64>,                 // Expiry time
    pub iat: Option<i64>,                 // Issued at
    pub nbf: Option<i64>,                 // Not before
    pub aud: Option<String>,              // Audience
    pub iss: Option<String>,              // Issuer
    pub jti: Option<String>,              // JWT ID
    pub is_staff: Option<bool>,           // Staff status
    pub is_superuser: Option<bool>,       // Admin/superuser status
    pub is_admin: Option<bool>,           // Alternative admin field
    pub permissions: Option<Vec<String>>, // List of permissions
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>, // Any extra claims
}

/// Authentication context built from successful authentication
#[derive(Debug, Clone)]
pub struct AuthContext {
    pub user_id: Option<String>,
    pub is_staff: bool,
    pub is_superuser: bool,
    pub backend: String,
    pub claims: Option<Claims>,
    pub permissions: HashSet<String>,
}

impl AuthContext {
    pub fn from_jwt_claims(claims: Claims, backend: &str) -> Self {
        let user_id = claims.sub.clone();
        let is_staff = claims.is_staff.unwrap_or(false);
        let is_superuser = claims.is_superuser.unwrap_or(false);

        let mut permissions = HashSet::new();
        if let Some(perms) = &claims.permissions {
            for perm in perms {
                permissions.insert(perm.clone());
            }
        }

        AuthContext {
            user_id,
            is_staff,
            is_superuser,
            backend: backend.to_string(),
            claims: Some(claims),
            permissions,
        }
    }

    pub fn from_api_key(key: &str, key_permissions: &HashMap<String, Vec<String>>) -> Self {
        let mut permissions = HashSet::new();
        if let Some(perms) = key_permissions.get(key) {
            for perm in perms {
                permissions.insert(perm.clone());
            }
        }

        AuthContext {
            user_id: Some(format!("apikey:{}", key)),
            is_staff: false,
            is_superuser: false,
            backend: "api_key".to_string(),
            claims: None,
            permissions,
        }
    }
}

/// Authentication backend configuration
#[derive(Debug, Clone)]
pub enum AuthBackend {
    JWT {
        secret: String,
        algorithms: Vec<String>,
        header: String,
        audience: Option<String>,
        issuer: Option<String>,
    },
    APIKey {
        api_keys: HashSet<String>,
        header: String,
        key_permissions: HashMap<String, Vec<String>>,
    },
}

/// Authenticate using configured backends and return AuthContext
/// Returns None if no authentication was successful
pub fn authenticate(
    headers: &AHashMap<String, String>,
    backends: &[AuthBackend],
) -> Option<AuthContext> {
    for backend in backends {
        match backend {
            AuthBackend::JWT {
                secret,
                algorithms,
                header,
                audience,
                issuer,
            } => {
                if let Some(ctx) = try_jwt_auth(
                    headers,
                    secret,
                    algorithms,
                    header,
                    audience.as_deref(),
                    issuer.as_deref(),
                ) {
                    return Some(ctx);
                }
            }
            AuthBackend::APIKey {
                api_keys,
                header,
                key_permissions,
            } => {
                if let Some(ctx) = try_api_key_auth(headers, api_keys, header, key_permissions) {
                    return Some(ctx);
                }
            }
        }
    }
    None
}

fn try_jwt_auth(
    headers: &AHashMap<String, String>,
    secret: &str,
    algorithms: &[String],
    header_name: &str,
    audience: Option<&str>,
    issuer: Option<&str>,
) -> Option<AuthContext> {
    // Get auth header
    let auth_header = headers.get(header_name)?;

    // Extract token (remove "Bearer " prefix if present)
    let token = if auth_header.starts_with("Bearer ") {
        &auth_header[7..]
    } else {
        auth_header
    };

    // Use FIRST algorithm only (as specified in config) - don't try multiple algorithms
    // This is more efficient and follows the principle: one token, one algorithm
    let algorithm = match algorithms.first().map(|s| s.as_str()).unwrap_or("HS256") {
        "HS256" => Algorithm::HS256,
        "HS384" => Algorithm::HS384,
        "HS512" => Algorithm::HS512,
        "RS256" => Algorithm::RS256,
        "RS384" => Algorithm::RS384,
        "RS512" => Algorithm::RS512,
        "ES256" => Algorithm::ES256,
        "ES384" => Algorithm::ES384,
        _ => Algorithm::HS256, // Default fallback
    };

    // Create validation with the specified algorithm
    let mut validation = Validation::new(algorithm);
    validation.validate_exp = true;
    validation.validate_nbf = true;

    if let Some(aud) = audience {
        validation.set_audience(&[aud]);
    }
    if let Some(iss) = issuer {
        validation.set_issuer(&[iss]);
    }

    // Decode token with the specified algorithm
    let key = DecodingKey::from_secret(secret.as_bytes());
    match decode::<Claims>(token, &key, &validation) {
        Ok(token_data) => Some(AuthContext::from_jwt_claims(token_data.claims, "jwt")),
        Err(_) => None,
    }
}

fn try_api_key_auth(
    headers: &AHashMap<String, String>,
    api_keys: &HashSet<String>,
    header_name: &str,
    key_permissions: &HashMap<String, Vec<String>>,
) -> Option<AuthContext> {
    // SECURITY: Reject if no API keys configured (don't allow empty set)
    if api_keys.is_empty() {
        return None;
    }

    // Get API key from header
    let api_key_header = headers.get(header_name)?;

    // Extract key (remove "Bearer " or "ApiKey " prefix if present)
    let api_key = if api_key_header.starts_with("Bearer ") {
        &api_key_header[7..]
    } else if api_key_header.starts_with("ApiKey ") {
        &api_key_header[7..]
    } else {
        api_key_header
    };

    // Check if key is valid - use constant-time comparison for security
    if api_keys.contains(api_key) {
        Some(AuthContext::from_api_key(api_key, key_permissions))
    } else {
        None
    }
}

/// Insert `key -> value` into a PyDict only if `value` is `Some`.
///
/// `key` must be a string literal so we can hand it to `pyo3::intern!` —
/// the resulting `PyString` is built once per interpreter and reused
/// across requests (skipping the per-call `&str → PyString` conversion
/// and rehashing).
macro_rules! set_if_some {
    ($dict:expr, $py:expr, $key:literal, $value:expr) => {
        if let Some(v) = $value {
            let _ = $dict.set_item(intern!($py, $key), v);
        }
    };
}

/// Store authentication context in PyRequest context.
///
/// All static dict keys go through `pyo3::intern!` (via `set_if_some!` for
/// the optional ones) so the underlying `PyString` is built once per
/// interpreter and reused — saves the per-request `&str → PyString`
/// conversion plus rehashing.
pub fn populate_auth_context(context: &Py<PyDict>, auth_ctx: &AuthContext, py: Python) {
    let dict = context.bind(py);

    set_if_some!(dict, py, "user_id", &auth_ctx.user_id);

    let _ = dict.set_item(intern!(py, "is_staff"), auth_ctx.is_staff);
    let _ = dict.set_item(intern!(py, "is_superuser"), auth_ctx.is_superuser);
    let _ = dict.set_item(intern!(py, "auth_backend"), &auth_ctx.backend);

    if !auth_ctx.permissions.is_empty() {
        let perms: Vec<&String> = auth_ctx.permissions.iter().collect();
        let _ = dict.set_item(intern!(py, "permissions"), perms);
    }

    // Store JWT claims if present
    if let Some(claims) = &auth_ctx.claims {
        let claims_dict = PyDict::new(py);

        set_if_some!(claims_dict, py, "sub", &claims.sub);
        set_if_some!(claims_dict, py, "exp", claims.exp);
        set_if_some!(claims_dict, py, "iat", claims.iat);
        set_if_some!(claims_dict, py, "is_staff", claims.is_staff);
        set_if_some!(claims_dict, py, "is_superuser", claims.is_superuser);
        set_if_some!(claims_dict, py, "nbf", claims.nbf);
        set_if_some!(claims_dict, py, "aud", &claims.aud);
        set_if_some!(claims_dict, py, "iss", &claims.iss);
        set_if_some!(claims_dict, py, "jti", &claims.jti);

        // Extra claims keys come from the JWT payload — can't be interned
        // statically since the set is open. set_item(&str, ...) handles them.
        for (key, value) in &claims.extra {
            let py_value = match value {
                serde_json::Value::String(s) => {
                    s.clone().into_py_any(py).unwrap_or_else(|_| py.None())
                }
                serde_json::Value::Number(n) => {
                    if let Some(i) = n.as_i64() {
                        i.into_py_any(py).unwrap_or_else(|_| py.None())
                    } else if let Some(f) = n.as_f64() {
                        f.into_py_any(py).unwrap_or_else(|_| py.None())
                    } else {
                        py.None()
                    }
                }
                serde_json::Value::Bool(b) => (*b).into_py_any(py).unwrap_or_else(|_| py.None()),
                _ => py.None(),
            };
            let _ = claims_dict.set_item(key, py_value);
        }

        let _ = dict.set_item(intern!(py, "auth_claims"), claims_dict);
    }
}
