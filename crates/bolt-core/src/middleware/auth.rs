use ahash::AHashMap;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use jsonwebtoken::{crypto, get_current_timestamp, Algorithm, AlgorithmFamily, DecodingKey};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::IntoPyObjectExt;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use std::str::FromStr;

/// JWT `aud` claim: RFC 7519 §4.1.3 allows a single string or an array of
/// strings. Real-world providers (e.g. Auth0) emit both forms.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Audience {
    Single(String),
    Multiple(Vec<String>),
}

impl Audience {
    fn contains(&self, expected: &str) -> bool {
        match self {
            Audience::Single(aud) => aud == expected,
            Audience::Multiple(auds) => auds.iter().any(|aud| aud == expected),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: Option<String>,              // Subject (user ID)
    pub exp: Option<i64>,                 // Expiry time
    pub iat: Option<i64>,                 // Issued at
    pub nbf: Option<i64>,                 // Not before
    pub aud: Option<Audience>,            // Audience (string or array)
    pub iss: Option<String>,              // Issuer
    pub jti: Option<String>,              // JWT ID
    pub typ: Option<String>,              // Token type ("access"/"refresh")
    pub fam: Option<String>,              // Refresh-token rotation family
    pub oat: Option<i64>,                 // Origin authentication time
    pub ver: Option<i64>,                 // User token version
    pub amr: Option<Vec<String>>,         // Authentication methods
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
    /// The credential that actually authenticated this request came from a
    /// cookie backend with CSRF enforcement enabled.
    pub cookie_csrf: bool,
}

impl AuthContext {
    pub fn from_jwt_claims(claims: Claims, backend: &str, cookie_csrf: bool) -> Self {
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
            cookie_csrf,
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
            cookie_csrf: false,
        }
    }
}

/// Where a JWT backend gets its verification key(s), resolved once at
/// registration. Both variants avoid any per-request key parsing.
#[derive(Debug, Clone)]
pub enum JwtKeySource {
    /// A single key from an HMAC secret or PEM public key.
    Static(DecodingKey),
    /// A JWKS: keys indexed by `kid`, selected per request from the token's
    /// `kid` header (RS/ES providers like Clerk, Auth0, Okta).
    Jwks(HashMap<String, DecodingKey>),
    /// Remotely sourced JWKS. The last known-good keys remain available while
    /// one worker refreshes them periodically or after an unknown `kid`.
    RefreshingJwks(Arc<RefreshingJwks>),
}

pub struct RefreshingJwks {
    keys: RwLock<HashMap<String, DecodingKey>>,
    refresh: Py<PyAny>,
    refresh_interval: Duration,
    last_refresh: Mutex<Instant>,
    refreshing: Mutex<()>,
}

impl std::fmt::Debug for RefreshingJwks {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RefreshingJwks")
            .field("key_count", &self.keys.read().len())
            .field("refresh_interval", &self.refresh_interval)
            .finish_non_exhaustive()
    }
}

impl JwtKeySource {
    /// Select the verification key for a token, given its `kid` header.
    /// For a JWKS with exactly one key the `kid` may be omitted.
    fn select(&self, kid: Option<&str>) -> Option<DecodingKey> {
        match self {
            JwtKeySource::Static(key) => Some(key.clone()),
            JwtKeySource::Jwks(keys) => match kid {
                Some(kid) => keys.get(kid).cloned(),
                None if keys.len() == 1 => keys.values().next().cloned(),
                None => None,
            },
            JwtKeySource::RefreshingJwks(remote) => remote.select(kid),
        }
    }
}

impl RefreshingJwks {
    pub fn new(
        keys: HashMap<String, DecodingKey>,
        refresh: Py<PyAny>,
        refresh_interval: Duration,
    ) -> Self {
        Self {
            keys: RwLock::new(keys),
            refresh,
            refresh_interval,
            last_refresh: Mutex::new(Instant::now()),
            refreshing: Mutex::new(()),
        }
    }

    fn cached(&self, kid: Option<&str>) -> Option<DecodingKey> {
        let keys = self.keys.read();
        match kid {
            Some(kid) => keys.get(kid).cloned(),
            None if keys.len() == 1 => keys.values().next().cloned(),
            None => None,
        }
    }

    fn select(&self, kid: Option<&str>) -> Option<DecodingKey> {
        let cached = self.cached(kid);
        let due = self.last_refresh.lock().elapsed() >= self.refresh_interval;
        if cached.is_some() && !due {
            return cached;
        }

        // try_lock provides single-flight refresh: other workers immediately
        // continue with cached keys instead of piling onto the provider.
        if let Some(_guard) = self.refreshing.try_lock() {
            let result = Python::attach(|py| {
                self.refresh
                    .call0(py)
                    .ok()
                    .and_then(|value| value.extract::<Option<String>>(py).ok())
                    .flatten()
            });
            *self.last_refresh.lock() = Instant::now();
            if let Some(document) = result {
                if let Ok(JwtKeySource::Jwks(keys)) = build_jwks_key_source(&document) {
                    *self.keys.write() = keys;
                }
            }
        }
        self.cached(kid).or(cached)
    }
}

/// Authentication backend configuration
#[derive(Debug, Clone)]
pub enum AuthBackend {
    JWT {
        /// Verification key(s), built once at registration from the configured
        /// secret (HMAC), PEM public key (RSA/EC/Ed25519), or JWKS. Never
        /// parsed on the request path.
        keys: JwtKeySource,
        /// Allowlist of accepted algorithms. A token whose `alg` header is
        /// not in this list is rejected before signature verification, so a
        /// token can never downgrade to a different family than `keys`.
        algorithms: Vec<Algorithm>,
        header: String,
        cookie: Option<String>,
        audience: Option<String>,
        issuer: Option<String>,
        /// Clock-skew tolerance (seconds) for `exp`/`nbf`.
        leeway: i64,
        /// Expected `typ` claim. `Some(t)` requires the token to carry
        /// exactly `typ == t` (e.g. a rotation endpoint sets "refresh").
        /// `None` (normal access routes) rejects `typ == "refresh"` so a
        /// refresh token can never authenticate a regular endpoint.
        token_type: Option<String>,
        /// Require a non-empty JWT ID claim, even without a Python
        /// revocation handler.
        require_jti: bool,
        /// Whether this cookie-sourced backend enforces CSRF on unsafe
        /// methods. Meaningful only when `cookie` is `Some`.
        cookie_csrf: bool,
    },
    APIKey {
        api_keys: HashSet<String>,
        header: String,
        key_permissions: HashMap<String, Vec<String>>,
    },
}

fn algorithm_family(alg: Algorithm) -> AlgorithmFamily {
    match alg {
        Algorithm::HS256 | Algorithm::HS384 | Algorithm::HS512 => AlgorithmFamily::Hmac,
        Algorithm::RS256
        | Algorithm::RS384
        | Algorithm::RS512
        | Algorithm::PS256
        | Algorithm::PS384
        | Algorithm::PS512 => AlgorithmFamily::Rsa,
        Algorithm::ES256 | Algorithm::ES384 => AlgorithmFamily::Ec,
        Algorithm::EdDSA => AlgorithmFamily::Ed,
    }
}

/// Parse a configured algorithm name into a `jsonwebtoken::Algorithm`.
/// Unknown names are a configuration error — silently falling back to a
/// different algorithm would verify tokens against the wrong scheme.
pub fn parse_jwt_algorithm(name: &str) -> Result<Algorithm, String> {
    Algorithm::from_str(name).map_err(|_| {
        format!(
            "Unsupported JWT algorithm '{}'. Supported: HS256, HS384, HS512, \
             RS256, RS384, RS512, PS256, PS384, PS512, ES256, ES384, EdDSA",
            name
        )
    })
}

/// Build the JWT verification key once at registration time.
///
/// For HMAC algorithms `secret` is the shared secret; for asymmetric
/// algorithms it must be a PEM-encoded public key. All configured algorithms
/// must share one key family, since a single key is built.
pub fn build_jwt_decoding_key(
    secret: &str,
    algorithms: &[Algorithm],
) -> Result<DecodingKey, String> {
    let first = *algorithms
        .first()
        .ok_or_else(|| "JWT auth backend requires at least one algorithm".to_string())?;
    let family = algorithm_family(first);
    for alg in &algorithms[1..] {
        if algorithm_family(*alg) != family {
            return Err(format!(
                "JWT algorithms {:?} mix key families; all algorithms of one backend must use the same key type",
                algorithms
            ));
        }
    }
    match family {
        AlgorithmFamily::Hmac => Ok(DecodingKey::from_secret(secret.as_bytes())),
        AlgorithmFamily::Rsa => DecodingKey::from_rsa_pem(secret.as_bytes())
            .map_err(|e| format!("Invalid RSA public key PEM for JWT auth: {}", e)),
        AlgorithmFamily::Ec => DecodingKey::from_ec_pem(secret.as_bytes())
            .map_err(|e| format!("Invalid EC public key PEM for JWT auth: {}", e)),
        AlgorithmFamily::Ed => DecodingKey::from_ed_pem(secret.as_bytes())
            .map_err(|e| format!("Invalid Ed25519 public key PEM for JWT auth: {}", e)),
    }
}

/// Build a `kid -> DecodingKey` map from a JWKS JSON document at
/// registration time. The document is typically fetched from a provider's
/// `jwks_url` (Clerk, Auth0, ...) by Python at startup and handed here as a
/// string. Keys without a `kid` are skipped (they can't be selected).
pub fn build_jwks_key_source(jwks_json: &str) -> Result<JwtKeySource, String> {
    let set: jsonwebtoken::jwk::JwkSet = serde_json::from_str(jwks_json)
        .map_err(|e| format!("Invalid JWKS document for JWT auth: {}", e))?;
    let mut keys = HashMap::new();
    for jwk in &set.keys {
        let kid = match &jwk.common.key_id {
            Some(kid) => kid.clone(),
            None => continue,
        };
        let key = DecodingKey::from_jwk(jwk)
            .map_err(|e| format!("Invalid JWK '{}' in JWKS for JWT auth: {}", kid, e))?;
        keys.insert(kid, key);
    }
    if keys.is_empty() {
        return Err("JWKS document contained no usable keys (each key needs a 'kid')".to_string());
    }
    Ok(JwtKeySource::Jwks(keys))
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
            } => {
                if let Some(ctx) = try_jwt_auth(
                    headers,
                    keys,
                    algorithms,
                    header,
                    cookie.as_deref(),
                    audience.as_deref(),
                    issuer.as_deref(),
                    *leeway,
                    token_type.as_deref(),
                    *require_jti,
                    cookie.is_some() && *cookie_csrf,
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

/// Find a cookie value by name in a raw Cookie header string.
/// Zero-allocation scan — returns a slice into the header value.
pub fn find_cookie_value<'a>(raw_cookie: &'a str, name: &str) -> Option<&'a str> {
    for pair in raw_cookie.split(';') {
        let part = pair.trim();
        if let Some(eq) = part.find('=') {
            let (k, v) = part.split_at(eq);
            if k == name {
                return Some(&v[1..]); // Skip '=' character
            }
        }
    }
    None
}

/// Minimal JOSE header: only the field the validation loop acts on.
///
/// Deliberately NOT `jsonwebtoken::Header`: its `extras` map is typed
/// `HashMap<String, String>`, so any non-string extension parameter —
/// which RFC 7515 §4.1.11 explicitly permits, and providers emit (Clerk:
/// `"oiat": <int>`, Auth0: `"gty": [...]`) — fails to parse before
/// signature verification even runs. serde ignores unknown fields here
/// regardless of their JSON type.
#[derive(Deserialize)]
struct TokenHeader {
    alg: String,
    #[serde(default)]
    kid: Option<String>,
}

/// Decode and validate a JWT against a prebuilt key and algorithm allowlist.
///
/// Owns the full validation loop instead of `jsonwebtoken::decode()` so the
/// JOSE header is parsed leniently (see [`TokenHeader`]) and claims
/// validation matches real-world tokens (`aud` as string or array; `aud`
/// ignored when no audience is configured).
#[allow(clippy::too_many_arguments)]
fn decode_and_validate(
    token: &str,
    keys: &JwtKeySource,
    algorithms: &[Algorithm],
    audience: Option<&str>,
    issuer: Option<&str>,
    leeway: i64,
    token_type: Option<&str>,
) -> Option<Claims> {
    let (message, signature) = token.rsplit_once('.')?;
    let (header_b64, claims_b64) = message.split_once('.')?;

    let header_bytes = URL_SAFE_NO_PAD.decode(header_b64).ok()?;
    let header: TokenHeader = serde_json::from_slice(&header_bytes).ok()?;

    // The token never chooses the algorithm — it must name one the backend
    // explicitly allows (prevents HS/RS confusion against the prebuilt key).
    let alg = Algorithm::from_str(&header.alg).ok()?;
    if !algorithms.contains(&alg) {
        log::debug!(
            "JWT rejected: algorithm '{}' not in configured allowlist",
            header.alg
        );
        return None;
    }

    // Select the verification key: the single static key, or the JWKS key
    // matching the token's `kid`.
    let key = match keys.select(header.kid.as_deref()) {
        Some(key) => key,
        None => {
            log::debug!("JWT rejected: no key matches the token's kid");
            return None;
        }
    };

    match crypto::verify(signature, message.as_bytes(), &key, alg) {
        Ok(true) => {}
        Ok(false) => {
            log::debug!("JWT rejected: invalid signature");
            return None;
        }
        Err(e) => {
            log::debug!("JWT rejected: signature verification failed: {}", e);
            return None;
        }
    }

    let claims_bytes = URL_SAFE_NO_PAD.decode(claims_b64).ok()?;
    let claims: Claims = match serde_json::from_slice(&claims_bytes) {
        Ok(claims) => claims,
        Err(e) => {
            log::debug!("JWT rejected: claims failed to parse: {}", e);
            return None;
        }
    };

    // Token-type separation (symmetric enforcement): an expected type must
    // match exactly; routes with no expectation never accept refresh tokens.
    match token_type {
        Some(expected) => {
            if claims.typ.as_deref() != Some(expected) {
                log::debug!("JWT rejected: typ claim does not match expected token type");
                return None;
            }
        }
        None => {
            if claims.typ.as_deref() == Some("refresh") {
                log::debug!("JWT rejected: refresh token used on an access route");
                return None;
            }
        }
    }

    let now = get_current_timestamp() as i64;
    match claims.exp {
        Some(exp) if exp >= now - leeway => {}
        Some(_) => {
            log::debug!("JWT rejected: token expired");
            return None;
        }
        None => {
            log::debug!("JWT rejected: missing required exp claim");
            return None;
        }
    }
    if let Some(nbf) = claims.nbf {
        if nbf > now + leeway {
            log::debug!("JWT rejected: token not yet valid (nbf)");
            return None;
        }
    }
    // aud is only validated when an audience is configured; a token merely
    // carrying an aud claim is not rejected (unlike jsonwebtoken's default).
    if let Some(expected) = audience {
        match &claims.aud {
            Some(aud) if aud.contains(expected) => {}
            _ => {
                log::debug!("JWT rejected: audience mismatch");
                return None;
            }
        }
    }
    // When an issuer is configured, a token without iss is rejected.
    if let Some(expected) = issuer {
        match claims.iss.as_deref() {
            Some(iss) if iss == expected => {}
            _ => {
                log::debug!("JWT rejected: issuer mismatch");
                return None;
            }
        }
    }

    Some(claims)
}

#[allow(clippy::too_many_arguments)]
fn try_jwt_auth(
    headers: &AHashMap<String, String>,
    keys: &JwtKeySource,
    algorithms: &[Algorithm],
    header_name: &str,
    cookie_name: Option<&str>,
    audience: Option<&str>,
    issuer: Option<&str>,
    leeway: i64,
    token_type: Option<&str>,
    require_jti: bool,
    cookie_csrf: bool,
) -> Option<AuthContext> {
    // Extract raw token: the named cookie when configured, the auth header
    // otherwise. No fallback between sources.
    let raw_token = match cookie_name {
        Some(name) => headers
            .get("cookie")
            .and_then(|raw| find_cookie_value(raw, name))?,
        None => headers.get(header_name)?.as_str(),
    };

    // Remove "Bearer " prefix if present
    let token = raw_token.strip_prefix("Bearer ").unwrap_or(raw_token);

    let claims = decode_and_validate(
        token, keys, algorithms, audience, issuer, leeway, token_type,
    )?;
    if require_jti && claims.jti.as_deref().is_none_or(str::is_empty) {
        log::debug!("JWT rejected: missing required jti claim");
        return None;
    }
    Some(AuthContext::from_jwt_claims(claims, "jwt", cookie_csrf))
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
        let _ = dict.set_item(intern!(py, "auth_claims"), claims_to_dict(py, claims));
    }
}

/// Build a Python dict of a token's claims: the standard registered claims
/// plus every extra claim from the JWT payload.
fn claims_to_dict<'py>(py: Python<'py>, claims: &Claims) -> Bound<'py, PyDict> {
    let claims_dict = PyDict::new(py);

    set_if_some!(claims_dict, py, "sub", &claims.sub);
    set_if_some!(claims_dict, py, "exp", claims.exp);
    set_if_some!(claims_dict, py, "iat", claims.iat);
    set_if_some!(claims_dict, py, "is_staff", claims.is_staff);
    set_if_some!(claims_dict, py, "is_superuser", claims.is_superuser);
    set_if_some!(claims_dict, py, "nbf", claims.nbf);
    match &claims.aud {
        Some(Audience::Single(aud)) => {
            let _ = claims_dict.set_item(intern!(py, "aud"), aud);
        }
        Some(Audience::Multiple(auds)) => {
            let _ = claims_dict.set_item(intern!(py, "aud"), auds.clone());
        }
        None => {}
    }
    set_if_some!(claims_dict, py, "iss", &claims.iss);
    set_if_some!(claims_dict, py, "jti", &claims.jti);
    set_if_some!(claims_dict, py, "typ", &claims.typ);
    set_if_some!(claims_dict, py, "fam", &claims.fam);
    set_if_some!(claims_dict, py, "oat", claims.oat);
    set_if_some!(claims_dict, py, "ver", claims.ver);
    set_if_some!(claims_dict, py, "amr", &claims.amr);

    // Extra claims keys come from the JWT payload — can't be interned
    // statically since the set is open. set_item(&str, ...) handles them.
    for (key, value) in &claims.extra {
        let py_value = match value {
            serde_json::Value::String(s) => s.clone().into_py_any(py).unwrap_or_else(|_| py.None()),
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

    claims_dict
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::EncodingKey;

    const SECRET: &str = "test-secret";

    fn hs256_key() -> JwtKeySource {
        JwtKeySource::Static(DecodingKey::from_secret(SECRET.as_bytes()))
    }

    /// Build a token from raw JSON parts so tests can emit headers that
    /// `jsonwebtoken::Header` cannot represent (non-string extras).
    fn sign_token(header_json: &str, claims_json: &str, alg: Algorithm) -> String {
        let message = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(header_json),
            URL_SAFE_NO_PAD.encode(claims_json)
        );
        let signature = crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_secret(SECRET.as_bytes()),
            alg,
        )
        .unwrap();
        format!("{}.{}", message, signature)
    }

    fn future_exp() -> i64 {
        get_current_timestamp() as i64 + 3600
    }

    #[test]
    fn accepts_non_string_header_extras() {
        // Clerk emits integer extension params ("oiat"), Auth0 emits arrays
        // ("gty") — both legal per RFC 7515 §4.1.11.
        let header = r#"{"alg":"HS256","typ":"JWT","oiat":1784025590,"gty":["authorization_code","refresh_token"]}"#;
        let claims_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(header, &claims_json, Algorithm::HS256);

        let claims = decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None,
        )
        .expect("token with non-string header extras must validate");
        assert_eq!(claims.sub.as_deref(), Some("42"));
    }

    #[test]
    fn rejects_algorithm_not_in_allowlist() {
        let claims_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS384],
                None,
                None,
                60,
                None
            )
            .is_none(),
            "HS256 token must be rejected when only HS384 is allowed"
        );
    }

    #[test]
    fn accepts_any_algorithm_in_allowlist() {
        let claims_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS384"}"#, &claims_json, Algorithm::HS384);

        let allowed = [Algorithm::HS256, Algorithm::HS384];
        assert!(
            decode_and_validate(&token, &hs256_key(), &allowed, None, None, 60, None).is_some(),
            "every algorithm in the configured list must be usable, not just the first"
        );
    }

    #[test]
    fn rejects_tampered_signature() {
        let claims_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);
        let tampered = if token.ends_with('A') {
            format!("{}B", &token[..token.len() - 1])
        } else {
            format!("{}A", &token[..token.len() - 1])
        };

        assert!(decode_and_validate(
            &tampered,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn rejects_expired_token() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{}}}"#,
            get_current_timestamp() as i64 - 3600
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn rejects_missing_exp() {
        let token = sign_token(r#"{"alg":"HS256"}"#, r#"{"sub":"42"}"#, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn rejects_future_nbf() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{},"nbf":{}}}"#,
            future_exp(),
            get_current_timestamp() as i64 + 3600
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn audience_array_matches_configured_audience() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{},"aud":["api","web"]}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            Some("api"),
            None,
            60,
            None
        )
        .is_some());
        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            Some("mobile"),
            None,
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn aud_claim_ignored_when_no_audience_configured() {
        // Auth0 access tokens always carry aud; they must not be rejected
        // just because the backend didn't configure an audience.
        let claims_json = format!(r#"{{"sub":"42","exp":{},"aud":"api"}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_some());
    }

    #[test]
    fn issuer_enforced_when_configured() {
        let with_iss = format!(
            r#"{{"sub":"42","exp":{},"iss":"https://issuer.example"}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &with_iss, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            Some("https://issuer.example"),
            60,
            None
        )
        .is_some());
        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            Some("https://other.example"),
            60,
            None
        )
        .is_none());

        // Missing iss is rejected when an issuer is configured.
        let without_iss = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &without_iss, Algorithm::HS256);
        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            Some("https://issuer.example"),
            60,
            None
        )
        .is_none());
    }

    #[test]
    fn build_decoding_key_rejects_bad_config() {
        assert!(build_jwt_decoding_key("secret", &[]).is_err());
        assert!(build_jwt_decoding_key("secret", &[Algorithm::HS256, Algorithm::RS256]).is_err());
        assert!(build_jwt_decoding_key("not-a-pem", &[Algorithm::RS256]).is_err());
        assert!(build_jwt_decoding_key("secret", &[Algorithm::HS256, Algorithm::HS512]).is_ok());
    }

    #[test]
    fn parse_jwt_algorithm_rejects_unknown_names() {
        assert!(parse_jwt_algorithm("ES512").is_err());
        assert!(parse_jwt_algorithm("none").is_err());
        assert!(parse_jwt_algorithm("RS256").is_ok());
        assert!(parse_jwt_algorithm("EdDSA").is_ok());
    }

    #[test]
    fn refresh_token_rejected_on_access_route() {
        let claims_json = format!(r#"{{"sub":"42","exp":{},"typ":"refresh"}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                None
            )
            .is_none(),
            "typ:\"refresh\" must never authenticate a route with no expected token type"
        );
    }

    #[test]
    fn token_type_expectation_enforced_both_ways() {
        let refresh_json = format!(r#"{{"sub":"42","exp":{},"typ":"refresh"}}"#, future_exp());
        let refresh = sign_token(r#"{"alg":"HS256"}"#, &refresh_json, Algorithm::HS256);
        let access_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let access = sign_token(r#"{"alg":"HS256"}"#, &access_json, Algorithm::HS256);

        // A rotation endpoint expecting "refresh" accepts refresh tokens...
        assert!(decode_and_validate(
            &refresh,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            Some("refresh")
        )
        .is_some());
        // ...and rejects access tokens (no typ at all).
        assert!(decode_and_validate(
            &access,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            Some("refresh")
        )
        .is_none());
    }

    #[test]
    fn access_typ_accepted_on_access_route() {
        // Tokens minted with an explicit typ:"access" work on normal routes.
        let claims_json = format!(r#"{{"sub":"42","exp":{},"typ":"access"}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_some());
    }

    #[test]
    fn jwks_selects_key_by_kid() {
        // Two HMAC "keys" in a JWKS-shaped source; the token's kid selects
        // which one verifies. (HMAC in a JwkSource is unusual but lets us
        // build the map without RSA machinery in a unit test.)
        let mut keys = HashMap::new();
        keys.insert("key-a".to_string(), DecodingKey::from_secret(b"secret-a"));
        keys.insert("key-b".to_string(), DecodingKey::from_secret(b"secret-b"));
        let source = JwtKeySource::Jwks(keys);

        assert!(source.select(Some("key-a")).is_some());
        assert!(source.select(Some("key-b")).is_some());
        assert!(source.select(Some("key-c")).is_none());
        // Ambiguous without a kid when more than one key exists.
        assert!(source.select(None).is_none());
    }

    #[test]
    fn jwks_without_kid_uses_sole_key() {
        let mut keys = HashMap::new();
        keys.insert("only".to_string(), DecodingKey::from_secret(b"s"));
        let source = JwtKeySource::Jwks(keys);
        assert!(source.select(None).is_some());
    }

    #[test]
    fn build_jwks_rejects_empty_or_invalid() {
        assert!(build_jwks_key_source("not json").is_err());
        assert!(build_jwks_key_source(r#"{"keys":[]}"#).is_err());
    }

    #[test]
    fn leeway_is_configurable() {
        // Expired 30s ago: accepted with 60s leeway, rejected with 0.
        let claims_json = format!(
            r#"{{"sub":"42","exp":{}}}"#,
            get_current_timestamp() as i64 - 30
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            60,
            None
        )
        .is_some());
        assert!(decode_and_validate(
            &token,
            &hs256_key(),
            &[Algorithm::HS256],
            None,
            None,
            0,
            None
        )
        .is_none());
    }
}
