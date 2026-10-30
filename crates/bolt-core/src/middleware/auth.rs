use ahash::AHashMap;
use aws_lc_rs::{constant_time, hmac};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use jsonwebtoken::{Algorithm, AlgorithmFamily, DecodingKey, crypto, get_current_timestamp};
use pyo3::IntoPyObjectExt;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, RandomState};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
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

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
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
    /// Position of the authenticating backend in the route's backend list.
    /// Two backends can have the same scheme name (two JWT secrets), so
    /// Python finds the per-backend revocation handler and user loader by
    /// this position. `None` when no route backend list made the context.
    pub backend_index: Option<usize>,
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
            backend_index: None,
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
            backend_index: None,
            claims: None,
            permissions,
            cookie_csrf: false,
        }
    }

    /// The context of a request that carries a Django session cookie.
    ///
    /// Rust cannot read a Django session, so the context has no user.
    /// Django loads the user, and Python checks the guards of the route.
    pub fn from_session(backend_index: usize, cookie_csrf: bool) -> Self {
        AuthContext {
            user_id: None,
            is_staff: false,
            is_superuser: false,
            backend: "session".to_string(),
            backend_index: Some(backend_index),
            claims: None,
            permissions: HashSet::new(),
            cookie_csrf,
        }
    }
}

/// Where a JWT backend gets its verification key(s), resolved once at
/// registration. Both variants avoid any per-request key parsing.
#[derive(Debug, Clone)]
pub enum JwtKeySource {
    /// A single key from an HMAC secret or PEM public key.
    Static(StaticKey),
    /// A JWKS: keys indexed by `kid`, selected per request from the token's
    /// `kid` header (RS/ES providers like Clerk, Auth0, Okta).
    Jwks(HashMap<String, DecodingKey>),
    /// Remotely sourced JWKS. The last known-good keys remain available while
    /// one worker refreshes them periodically or after an unknown `kid`.
    RefreshingJwks(Arc<RefreshingJwks>),
}

/// A static verification key. For an HMAC secret it also holds an HMAC key
/// for each HMAC algorithm, built once. `crypto::verify` builds one for each
/// token.
#[derive(Clone)]
pub struct StaticKey {
    key: DecodingKey,
    hmac: Option<Arc<HmacKeys>>,
}

struct HmacKeys {
    hs256: hmac::Key,
    hs384: hmac::Key,
    hs512: hmac::Key,
}

impl StaticKey {
    pub fn new(key: DecodingKey) -> Self {
        // Only an HMAC secret gets HMAC keys. A public key also keeps its
        // bytes, and they must never verify an HMAC token.
        let secret = match key.family() {
            AlgorithmFamily::Hmac => key.try_get_as_bytes().ok(),
            _ => None,
        };
        let hmac = secret.map(|secret| {
            Arc::new(HmacKeys {
                hs256: hmac::Key::new(hmac::HMAC_SHA256, secret),
                hs384: hmac::Key::new(hmac::HMAC_SHA384, secret),
                hs512: hmac::Key::new(hmac::HMAC_SHA512, secret),
            })
        });
        Self { key, hmac }
    }

    /// The prebuilt key for `alg`. None for an asymmetric key or algorithm.
    fn hmac_key(&self, alg: Algorithm) -> Option<&hmac::Key> {
        let keys = self.hmac.as_deref()?;
        match alg {
            Algorithm::HS256 => Some(&keys.hs256),
            Algorithm::HS384 => Some(&keys.hs384),
            Algorithm::HS512 => Some(&keys.hs512),
            _ => None,
        }
    }
}

impl std::fmt::Debug for StaticKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StaticKey")
            .field("family", &self.key.family())
            .finish_non_exhaustive()
    }
}

pub struct RefreshingJwks {
    keys: RwLock<HashMap<String, DecodingKey>>,
    refresh: Py<PyAny>,
    refresh_interval: Duration,
    last_refresh: Mutex<Instant>,
    /// The last fetch that a token with an unknown kid started.
    last_unknown_kid_fetch: Mutex<Option<Instant>>,
    /// True while a fetch runs. Only one fetch runs at a time.
    refreshing: AtomicBool,
}

/// Clears the fetch flag when the fetch ends, also on a panic.
struct FetchFlag<'a>(&'a AtomicBool);

impl Drop for FetchFlag<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// After a fetch for an unknown kid, other unknown kids wait this long.
/// A token with a made-up kid needs no valid signature to get here, so
/// without a wait each such request fetched the JWKS from the provider.
const UNKNOWN_KID_FETCH_COOLDOWN: Duration = Duration::from_secs(30);

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
            JwtKeySource::Static(key) => Some(key.key.clone()),
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
            last_unknown_kid_fetch: Mutex::new(None),
            refreshing: AtomicBool::new(false),
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

    fn select(self: &Arc<Self>, kid: Option<&str>) -> Option<DecodingKey> {
        let cached = self.cached(kid);
        if cached.is_some() {
            // The periodic refresh runs on its own thread. The request keeps
            // the cached key, so no request waits for the provider.
            if self.last_refresh.lock().elapsed() >= self.refresh_interval {
                self.fetch_in_background();
            }
            return cached;
        }

        // While another fetch runs, the request does not wait for it, and
        // the cooldown does not start.
        if self
            .refreshing
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            return None;
        }
        let _flag = FetchFlag(&self.refreshing);
        // The first unknown kid fetches at once, for a key rotation, and the
        // request waits for the new keys. Later unknown kids fetch again only
        // after the cooldown, also when the interval ends.
        {
            let mut last_fetch = self.last_unknown_kid_fetch.lock();
            if last_fetch.is_some_and(|at| at.elapsed() < UNKNOWN_KID_FETCH_COOLDOWN) {
                return None;
            }
            *last_fetch = Some(Instant::now());
        }
        self.fetch();
        self.cached(kid)
    }

    /// Start a fetch on a new thread, unless a fetch runs.
    fn fetch_in_background(self: &Arc<Self>) {
        if self
            .refreshing
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            return;
        }
        let jwks = Arc::clone(self);
        let spawned = std::thread::Builder::new()
            .name("bolt-jwks-refresh".to_string())
            .spawn(move || {
                let _flag = FetchFlag(&jwks.refreshing);
                jwks.fetch();
            });
        if let Err(error) = spawned {
            self.refreshing.store(false, Ordering::Release);
            log::warn!("JWKS refresh thread did not start: {error}");
        }
    }

    /// Get the JWKS from the Python callback and replace the keys. A failed
    /// fetch keeps the last known-good keys.
    fn fetch(&self) {
        let result = Python::attach(|py| {
            self.refresh
                .call0(py)
                .ok()
                .and_then(|value| value.extract::<Option<String>>(py).ok())
                .flatten()
        });
        *self.last_refresh.lock() = Instant::now();
        if let Some(document) = result
            && let Ok(JwtKeySource::Jwks(keys)) = build_jwks_key_source(&document)
        {
            *self.keys.write() = keys;
        }
    }
}

/// The valid keys of an API key backend, by their SipHash.
///
/// The SipHash key is random for each `ApiKeys`, so a client cannot compute
/// the hash of a key. `find` looks up the hash of the request key, and then
/// compares the request key in constant time with each valid key of that
/// hash. Thus the lookup compares only hashes, never the bytes of a key, and
/// the cost does not grow with the number of keys.
#[derive(Debug, Clone, Default)]
pub struct ApiKeys {
    state: RandomState,
    by_hash: AHashMap<u64, Vec<String>>,
}

impl ApiKeys {
    /// The valid key that equals `candidate`, or None.
    pub fn find(&self, candidate: &str) -> Option<&str> {
        let keys = self.by_hash.get(&self.state.hash_one(candidate))?;
        // Two keys can have the same hash. Compare each, with no early stop.
        let mut found = None;
        for key in keys {
            if constant_time::verify_slices_are_equal(key.as_bytes(), candidate.as_bytes()).is_ok()
            {
                found = Some(key.as_str());
            }
        }
        found
    }
}

impl FromIterator<String> for ApiKeys {
    fn from_iter<I: IntoIterator<Item = String>>(keys: I) -> Self {
        let state = RandomState::new();
        let mut by_hash: AHashMap<u64, Vec<String>> = AHashMap::new();
        for key in keys {
            let same_hash = by_hash.entry(state.hash_one(key.as_str())).or_default();
            if !same_hash.contains(&key) {
                same_hash.push(key);
            }
        }
        Self { state, by_hash }
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
        /// exactly `typ == t` (e.g. a rotation endpoint sets "refresh"),
        /// and no other token type claim that disagrees. `None` (normal
        /// access routes) rejects a token whose `typ`, `token_type` or
        /// `token_use` names a refresh or ID token (see
        /// `NON_ACCESS_TOKEN_TYPES`), so such a token can never
        /// authenticate a regular endpoint.
        token_type: Option<String>,
        /// Require a non-empty JWT ID claim, even without a Python
        /// revocation handler.
        require_jti: bool,
        /// Whether this cookie-sourced backend enforces CSRF on unsafe
        /// methods. Meaningful only when `cookie` is `Some`.
        cookie_csrf: bool,
    },
    APIKey {
        api_keys: ApiKeys,
        header: String,
        key_permissions: HashMap<String, Vec<String>>,
    },
    /// Django session authentication. Rust only finds the session cookie.
    /// Django's middleware loads the session and the user (see
    /// `session_context`).
    Session {
        /// Django's `SESSION_COOKIE_NAME`, read at registration.
        cookie: String,
        /// Enforce the CSRF origin check on unsafe methods.
        csrf: bool,
        /// The backend redirects a request with no logged-in user to a login
        /// page. Python builds the redirect, so a request with no cookie also
        /// gets a session context.
        login_redirect: bool,
    },
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
    let family = first.family();
    for alg in &algorithms[1..] {
        if alg.family() != family {
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

/// Authenticate using configured backends and return AuthContext.
/// Returns None if no authentication was successful. The context records
/// the position of the backend that accepted the credential.
///
/// A session backend never authenticates here. Rust cannot verify a
/// session, so a token that Rust can verify comes first. See
/// `session_context`.
pub fn authenticate(
    headers: &AHashMap<String, String>,
    backends: &[AuthBackend],
) -> Option<AuthContext> {
    backends.iter().enumerate().find_map(|(index, backend)| {
        let mut ctx = authenticate_with(headers, backend)?;
        ctx.backend_index = Some(index);
        Some(ctx)
    })
}

fn authenticate_with(
    headers: &AHashMap<String, String>,
    backend: &AuthBackend,
) -> Option<AuthContext> {
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
        } => try_jwt_auth(
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
        ),
        AuthBackend::APIKey {
            api_keys,
            header,
            key_permissions,
        } => try_api_key_auth(headers, api_keys, header, key_permissions),
        AuthBackend::Session { .. } => None,
    }
}

/// The session context of a request on a route with a session backend.
///
/// Rust cannot read a Django session, so the context has no user. The first
/// session backend of the route gives the context:
///
/// - When the guards cannot reject, always. Its CSRF flag then covers every
///   unsafe request, also with no cookie, so a cross-site page cannot log a
///   browser in.
/// - When the guards can reject, only for a request that Django must decide:
///   one with a session cookie, or any request when the backend redirects to
///   a login page. Other requests get 401 in Rust.
pub fn session_context(
    headers: &AHashMap<String, String>,
    backends: &[AuthBackend],
    guards_enforcing: bool,
) -> Option<AuthContext> {
    let (index, cookie, csrf, login_redirect) =
        backends
            .iter()
            .enumerate()
            .find_map(|(index, backend)| match backend {
                AuthBackend::Session {
                    cookie,
                    csrf,
                    login_redirect,
                } => Some((index, cookie, *csrf, *login_redirect)),
                _ => None,
            })?;
    let defer = !guards_enforcing
        || login_redirect
        || headers
            .get("cookie")
            .is_some_and(|raw| has_cookie_value(raw, cookie));
    defer.then(|| AuthContext::from_session(index, csrf))
}

/// Whether any pair of a raw Cookie header has this name and a value.
///
/// Django reads the last pair of a name, Rust code elsewhere the first. An
/// empty pair must not hide another pair, so every pair counts.
fn has_cookie_value(raw_cookie: &str, name: &str) -> bool {
    raw_cookie.split(';').any(|pair| {
        pair.trim()
            .split_once('=')
            .is_some_and(|(key, value)| key == name && !value.is_empty())
    })
}

/// Find a cookie value by name in a raw Cookie header string, as
/// `request.cookies` reads the header. A name that occurs two times gives
/// None: a sibling subdomain can add a cookie with the same name, and the
/// order of the two does not show which one the site set. A token must not
/// come from such a choice. The value borrows from the header unless it has
/// quotes to remove.
pub fn find_cookie_value<'a>(raw_cookie: &'a str, name: &str) -> Option<Cow<'a, str>> {
    let mut values = crate::cookies::raw_cookie_pairs(raw_cookie)
        .filter(|(cookie_name, _)| *cookie_name == name)
        .map(|(_, value)| value);
    match (values.next(), values.next()) {
        (Some(value), None) => Some(crate::cookies::unquote_cookie_value(value)),
        _ => None,
    }
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

/// Claims other than `typ` that name the token type: `token_type`
/// (djangorestframework-simplejwt) and `token_use` (django-allauth, AWS Cognito).
const OTHER_TOKEN_TYPE_CLAIMS: [&str; 2] = ["token_type", "token_use"];

/// Token types that never authenticate a route with no expected token type,
/// compared without case: refresh tokens (`refresh`; Keycloak `Refresh` and
/// `Offline`) and ID tokens (AWS Cognito `id`; Keycloak `ID`).
const NON_ACCESS_TOKEN_TYPES: [&str; 3] = ["refresh", "offline", "id"];

fn is_non_access_token_type(token_type: &str) -> bool {
    NON_ACCESS_TOKEN_TYPES
        .iter()
        .any(|name| token_type.eq_ignore_ascii_case(name))
}

/// The token types that the claims declare: `typ`, then each string value of
/// [`OTHER_TOKEN_TYPE_CLAIMS`].
fn declared_token_types(claims: &Claims) -> impl Iterator<Item = &str> {
    claims.typ.as_deref().into_iter().chain(
        OTHER_TOKEN_TYPE_CLAIMS
            .iter()
            .filter_map(|name| claims.extra.get(*name).and_then(serde_json::Value::as_str)),
    )
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

    // A static HMAC secret verifies with its prebuilt key, as crypto::verify
    // does: decode the signature, then compare its HMAC in constant time.
    // Other keys come from the static key or from the JWKS key of the
    // token's `kid`.
    let prebuilt = match keys {
        JwtKeySource::Static(key) => key.hmac_key(alg),
        _ => None,
    };
    let verified = match prebuilt {
        Some(hmac_key) => URL_SAFE_NO_PAD
            .decode(signature)
            .map(|signature| hmac::verify(hmac_key, message.as_bytes(), &signature).is_ok())
            .map_err(|e| e.to_string()),
        None => {
            let Some(key) = keys.select(header.kid.as_deref()) else {
                log::debug!("JWT rejected: no key matches the token's kid");
                return None;
            };
            crypto::verify(signature, message.as_bytes(), &key, alg).map_err(|e| e.to_string())
        }
    };

    match verified {
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
    // match `typ` exactly; routes with no expectation never accept refresh
    // or ID tokens. Other issuers name the type in other claims, so each of
    // those counts too: such a token never passes as an access token.
    match token_type {
        Some(expected) => {
            if claims.typ.as_deref() != Some(expected)
                || declared_token_types(&claims).any(|t| t != expected)
            {
                log::debug!("JWT rejected: token type claims do not match expected token type");
                return None;
            }
        }
        None => {
            if declared_token_types(&claims).any(is_non_access_token_type) {
                log::debug!("JWT rejected: refresh or ID token used on an access route");
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
    if let Some(nbf) = claims.nbf
        && nbf > now + leeway
    {
        log::debug!("JWT rejected: token not yet valid (nbf)");
        return None;
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
        None => Cow::Borrowed(headers.get(header_name)?.as_str()),
    };

    // Remove "Bearer " prefix if present
    let token = raw_token.strip_prefix("Bearer ").unwrap_or(&raw_token);

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
    api_keys: &ApiKeys,
    header_name: &str,
    key_permissions: &HashMap<String, Vec<String>>,
) -> Option<AuthContext> {
    // SECURITY: Reject if no API keys configured (don't allow empty set)
    if api_keys.by_hash.is_empty() {
        return None;
    }

    // Get API key from header
    let api_key_header = headers.get(header_name)?;

    // Extract key (remove "Bearer " or "ApiKey " prefix if present)
    let api_key = api_key_header
        .strip_prefix("Bearer ")
        .or_else(|| api_key_header.strip_prefix("ApiKey "))
        .unwrap_or(api_key_header);

    // Check if the key is valid. See `ApiKeys::find` for the timing.
    let api_key = api_keys.find(api_key)?;
    Some(AuthContext::from_api_key(api_key, key_permissions))
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
    set_if_some!(dict, py, "auth_backend_index", auth_ctx.backend_index);

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

    #[test]
    fn a_cookie_name_that_occurs_two_times_gives_no_value() {
        // A sibling subdomain can add a cookie with the same name, and the
        // order of the two does not show which one the site set.
        assert_eq!(find_cookie_value("tok=a; tok=b", "tok"), None);
        assert_eq!(
            find_cookie_value("tok=a; other=b", "tok").as_deref(),
            Some("a")
        );
        assert_eq!(find_cookie_value("other=b", "tok"), None);
    }
    use jsonwebtoken::EncodingKey;

    const SECRET: &str = "test-secret";

    fn hs256_key() -> JwtKeySource {
        JwtKeySource::Static(StaticKey::new(DecodingKey::from_secret(SECRET.as_bytes())))
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

        assert!(
            decode_and_validate(
                &tampered,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                None
            )
            .is_none()
        );
    }

    #[test]
    fn an_hmac_secret_has_a_prebuilt_key_for_each_hmac_algorithm() {
        let JwtKeySource::Static(secret) = hs256_key() else {
            unreachable!()
        };
        for alg in [Algorithm::HS256, Algorithm::HS384, Algorithm::HS512] {
            assert!(secret.hmac_key(alg).is_some(), "{alg:?}");
        }
        assert!(secret.hmac_key(Algorithm::RS256).is_none());

        let rsa = StaticKey::new(DecodingKey::from_rsa_components("AQAB", "AQAB").unwrap());
        assert!(rsa.hmac_key(Algorithm::RS256).is_none());
        assert!(rsa.hmac_key(Algorithm::HS256).is_none());

        // A public key from a PEM or a DER file keeps its bytes as a secret
        // does. They must not become an HMAC key: a token signed with the
        // public key as an HMAC secret would then verify.
        for key in [
            DecodingKey::from_rsa_der(b"der"),
            DecodingKey::from_ec_der(b"der"),
            DecodingKey::from_ed_der(b"der"),
        ] {
            let public = StaticKey::new(key);
            assert!(public.hmac_key(Algorithm::HS256).is_none(), "{public:?}");
        }
    }

    #[test]
    fn the_prebuilt_hmac_keys_accept_only_a_token_of_the_same_secret() {
        let claims_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let allowed = [Algorithm::HS256, Algorithm::HS384, Algorithm::HS512];
        let other = JwtKeySource::Static(StaticKey::new(DecodingKey::from_secret(b"other")));
        let validate = |token: &str, keys: &JwtKeySource| {
            decode_and_validate(token, keys, &allowed, None, None, 60, None)
        };

        for (alg, name) in [
            (Algorithm::HS256, "HS256"),
            (Algorithm::HS384, "HS384"),
            (Algorithm::HS512, "HS512"),
        ] {
            let token = sign_token(&format!(r#"{{"alg":"{name}"}}"#), &claims_json, alg);
            let (message, signature) = token.rsplit_once('.').unwrap();
            let mut bytes = URL_SAFE_NO_PAD.decode(signature).unwrap();
            bytes[0] ^= 1;
            let tampered = format!("{message}.{}", URL_SAFE_NO_PAD.encode(bytes));
            let not_base64 = format!("{message}.not*base64");

            assert!(validate(&token, &hs256_key()).is_some(), "{name}");
            assert!(validate(&token, &other).is_none(), "{name}");
            assert!(validate(&tampered, &hs256_key()).is_none(), "{name}");
            assert!(validate(&not_base64, &hs256_key()).is_none(), "{name}");
        }
    }

    #[test]
    fn rejects_expired_token() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{}}}"#,
            get_current_timestamp() as i64 - 3600
        );
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
            .is_none()
        );
    }

    #[test]
    fn rejects_missing_exp() {
        let token = sign_token(r#"{"alg":"HS256"}"#, r#"{"sub":"42"}"#, Algorithm::HS256);

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
            .is_none()
        );
    }

    #[test]
    fn rejects_future_nbf() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{},"nbf":{}}}"#,
            future_exp(),
            get_current_timestamp() as i64 + 3600
        );
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
            .is_none()
        );
    }

    #[test]
    fn audience_array_matches_configured_audience() {
        let claims_json = format!(
            r#"{{"sub":"42","exp":{},"aud":["api","web"]}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);

        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                Some("api"),
                None,
                60,
                None
            )
            .is_some()
        );
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                Some("mobile"),
                None,
                60,
                None
            )
            .is_none()
        );
    }

    #[test]
    fn aud_claim_ignored_when_no_audience_configured() {
        // Auth0 access tokens always carry aud; they must not be rejected
        // just because the backend didn't configure an audience.
        let claims_json = format!(r#"{{"sub":"42","exp":{},"aud":"api"}}"#, future_exp());
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
            .is_some()
        );
    }

    #[test]
    fn issuer_enforced_when_configured() {
        let with_iss = format!(
            r#"{{"sub":"42","exp":{},"iss":"https://issuer.example"}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &with_iss, Algorithm::HS256);

        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                Some("https://issuer.example"),
                60,
                None
            )
            .is_some()
        );
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                Some("https://other.example"),
                60,
                None
            )
            .is_none()
        );

        // Missing iss is rejected when an issuer is configured.
        let without_iss = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &without_iss, Algorithm::HS256);
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                Some("https://issuer.example"),
                60,
                None
            )
            .is_none()
        );
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
    fn non_access_token_types_rejected_on_access_route_without_case() {
        // Keycloak: typ Refresh, Offline, ID. AWS Cognito: token_use id.
        for (claim, value) in [
            ("typ", "Refresh"),
            ("typ", "Offline"),
            ("typ", "ID"),
            ("token_use", "id"),
            ("token_type", "REFRESH"),
        ] {
            let json = format!(
                r#"{{"sub":"42","exp":{},"{}":"{}"}}"#,
                future_exp(),
                claim,
                value
            );
            let token = sign_token(r#"{"alg":"HS256"}"#, &json, Algorithm::HS256);
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
                "{claim}:{value} must never authenticate a route with no expected token type"
            );
        }
        // Keycloak access tokens carry typ Bearer.
        let json = format!(r#"{{"sub":"42","exp":{},"typ":"Bearer"}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &json, Algorithm::HS256);
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
            .is_some()
        );
    }

    #[test]
    fn refresh_type_in_any_type_claim_rejected_on_access_route() {
        for claim in ["typ", "token_type", "token_use"] {
            let refresh_json = format!(
                r#"{{"sub":"42","exp":{},"{}":"refresh"}}"#,
                future_exp(),
                claim
            );
            let refresh = sign_token(r#"{"alg":"HS256"}"#, &refresh_json, Algorithm::HS256);
            let access_json = format!(
                r#"{{"sub":"42","exp":{},"{}":"access"}}"#,
                future_exp(),
                claim
            );
            let access = sign_token(r#"{"alg":"HS256"}"#, &access_json, Algorithm::HS256);

            assert!(
                decode_and_validate(
                    &refresh,
                    &hs256_key(),
                    &[Algorithm::HS256],
                    None,
                    None,
                    60,
                    None
                )
                .is_none(),
                "{claim}:\"refresh\" must never authenticate a route with no expected token type"
            );
            assert!(
                decode_and_validate(
                    &access,
                    &hs256_key(),
                    &[Algorithm::HS256],
                    None,
                    None,
                    60,
                    None
                )
                .is_some(),
                "{claim}:\"access\" authenticates a route with no expected token type"
            );
        }
    }

    #[test]
    fn expected_token_type_rejects_a_conflicting_type_claim() {
        let json = format!(
            r#"{{"sub":"42","exp":{},"typ":"refresh","token_use":"access"}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &json, Algorithm::HS256);
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                Some("refresh")
            )
            .is_none()
        );
        // An expected type still needs `typ`: another type claim alone is not enough.
        let json = format!(
            r#"{{"sub":"42","exp":{},"token_type":"refresh"}}"#,
            future_exp()
        );
        let token = sign_token(r#"{"alg":"HS256"}"#, &json, Algorithm::HS256);
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                Some("refresh")
            )
            .is_none()
        );
    }

    #[test]
    fn token_type_expectation_enforced_both_ways() {
        let refresh_json = format!(r#"{{"sub":"42","exp":{},"typ":"refresh"}}"#, future_exp());
        let refresh = sign_token(r#"{"alg":"HS256"}"#, &refresh_json, Algorithm::HS256);
        let access_json = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let access = sign_token(r#"{"alg":"HS256"}"#, &access_json, Algorithm::HS256);

        // A rotation endpoint expecting "refresh" accepts refresh tokens...
        assert!(
            decode_and_validate(
                &refresh,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                Some("refresh")
            )
            .is_some()
        );
        // ...and rejects access tokens (no typ at all).
        assert!(
            decode_and_validate(
                &access,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                60,
                Some("refresh")
            )
            .is_none()
        );
    }

    #[test]
    fn access_typ_accepted_on_access_route() {
        // Tokens minted with an explicit typ:"access" work on normal routes.
        let claims_json = format!(r#"{{"sub":"42","exp":{},"typ":"access"}}"#, future_exp());
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
            .is_some()
        );
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
            .is_some()
        );
        assert!(
            decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                None,
                0,
                None
            )
            .is_none()
        );
    }

    fn validate(token: &str, audience: Option<&str>, leeway: i64) -> Option<Claims> {
        decode_and_validate(
            token,
            &hs256_key(),
            &[Algorithm::HS256],
            audience,
            None,
            leeway,
            None,
        )
    }

    #[test]
    fn a_single_audience_must_be_the_configured_one() {
        let claims_json = format!(r#"{{"sub":"42","exp":{},"aud":"api"}}"#, future_exp());
        let token = sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256);
        assert!(validate(&token, Some("api"), 60).is_some());
        assert!(validate(&token, Some("web"), 60).is_none());
    }

    #[test]
    fn leeway_also_applies_to_nbf() {
        let now = get_current_timestamp() as i64;
        let token_valid_from = |nbf: i64| {
            let claims_json = format!(r#"{{"sub":"42","exp":{},"nbf":{nbf}}}"#, future_exp());
            sign_token(r#"{"alg":"HS256"}"#, &claims_json, Algorithm::HS256)
        };
        // Valid 30s from now: accepted with 60s leeway, rejected with 0.
        assert!(validate(&token_valid_from(now + 30), None, 60).is_some());
        assert!(validate(&token_valid_from(now + 30), None, 0).is_none());
        // Valid at the end of the leeway: accepted.
        assert!(validate(&token_valid_from(now + 60), None, 60).is_some());
    }

    #[test]
    fn a_cookie_value_is_read_as_request_cookies_reads_it() {
        assert_eq!(
            find_cookie_value(r#"a=1; tok="y z""#, "tok").as_deref(),
            Some("y z")
        );
        assert_eq!(find_cookie_value(" tok = x ", "tok").as_deref(), Some("x"));
        assert_eq!(find_cookie_value("a=1; tokx=2; atok=3", "tok"), None);
    }

    #[test]
    fn only_a_cookie_backend_enforces_cookie_csrf() {
        let headers = bearer_headers(SECRET);
        let token = headers["authorization"]
            .strip_prefix("Bearer ")
            .unwrap()
            .to_string();
        let backend = |cookie: Option<&str>| match hs256_backend(SECRET) {
            AuthBackend::JWT {
                keys,
                algorithms,
                header,
                ..
            } => AuthBackend::JWT {
                keys,
                algorithms,
                header,
                cookie: cookie.map(str::to_string),
                audience: None,
                issuer: None,
                leeway: 0,
                token_type: None,
                require_jti: false,
                cookie_csrf: true,
            },
            AuthBackend::APIKey { .. } | AuthBackend::Session { .. } => {
                unreachable!("hs256_backend is a JWT backend")
            }
        };

        let ctx = authenticate(&headers, &[backend(None)]).unwrap();
        assert!(!ctx.cookie_csrf);

        let cookies =
            AHashMap::from_iter([("cookie".to_string(), format!("theme=dark; access={token}"))]);
        let ctx = authenticate(&cookies, &[backend(Some("access"))]).unwrap();
        assert!(ctx.cookie_csrf);
    }

    fn hs256_backend(secret: &str) -> AuthBackend {
        AuthBackend::JWT {
            keys: JwtKeySource::Static(StaticKey::new(DecodingKey::from_secret(secret.as_bytes()))),
            algorithms: vec![Algorithm::HS256],
            header: "authorization".to_string(),
            cookie: None,
            audience: None,
            issuer: None,
            leeway: 0,
            token_type: None,
            require_jti: false,
            cookie_csrf: false,
        }
    }

    fn bearer_headers(secret: &str) -> AHashMap<String, String> {
        let claims = format!(r#"{{"sub":"42","exp":{}}}"#, future_exp());
        let message = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256"}"#),
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
    fn authenticate_records_the_position_of_the_accepting_backend() {
        // Both backends have the scheme name "jwt". Only the position tells
        // Python which backend accepted the token.
        let backends = [hs256_backend("secret-a"), hs256_backend("secret-b")];

        let first = authenticate(&bearer_headers("secret-a"), &backends).unwrap();
        assert_eq!(
            (first.backend.as_str(), first.backend_index),
            ("jwt", Some(0))
        );

        let second = authenticate(&bearer_headers("secret-b"), &backends).unwrap();
        assert_eq!(
            (second.backend.as_str(), second.backend_index),
            ("jwt", Some(1))
        );

        assert!(authenticate(&bearer_headers("secret-c"), &backends).is_none());
    }

    #[test]
    fn an_api_key_must_equal_a_valid_key() {
        let keys: ApiKeys = ["key-one".to_string(), "key-two".to_string()]
            .into_iter()
            .collect();
        assert_eq!(keys.find("key-one"), Some("key-one"));
        assert_eq!(keys.find("key-two"), Some("key-two"));
        for candidate in ["", "key-on", "key-one ", "key-onf", "KEY-ONE", "key-three"] {
            assert_eq!(keys.find(candidate), None, "{candidate:?}");
        }
        assert_eq!(ApiKeys::default().find(""), None);

        let repeated: ApiKeys = ["b", "a", "b"].into_iter().map(str::to_string).collect();
        let mut stored: Vec<&String> = repeated.by_hash.values().flatten().collect();
        stored.sort();
        assert_eq!(stored, ["a", "b"]);

        let many: ApiKeys = (0..100).map(|i| format!("key-{i}")).collect();
        for i in 0..100 {
            let key = format!("key-{i}");
            assert_eq!(many.find(&key), Some(key.as_str()));
        }
        assert_eq!(many.find("key-100"), None);
    }

    #[test]
    fn an_api_key_backend_strips_the_prefix_and_names_the_key() {
        let backends = [AuthBackend::APIKey {
            api_keys: ["key-one".to_string()].into_iter().collect(),
            header: "x-api-key".to_string(),
            key_permissions: HashMap::from([("key-one".to_string(), vec!["read".to_string()])]),
        }];
        for value in ["key-one", "Bearer key-one", "ApiKey key-one"] {
            let headers = AHashMap::from_iter([("x-api-key".to_string(), value.to_string())]);
            let ctx = authenticate(&headers, &backends).unwrap();
            assert_eq!(ctx.user_id.as_deref(), Some("apikey:key-one"));
            assert!(ctx.permissions.contains("read"));
        }
        for value in ["key-two", "Token key-one", ""] {
            let headers = AHashMap::from_iter([("x-api-key".to_string(), value.to_string())]);
            assert!(authenticate(&headers, &backends).is_none(), "{value:?}");
        }
    }

    #[test]
    fn authenticate_records_the_position_of_an_api_key_backend() {
        let backends = [
            hs256_backend("secret-a"),
            AuthBackend::APIKey {
                api_keys: ["key-1".to_string()].into_iter().collect(),
                header: "x-api-key".to_string(),
                key_permissions: HashMap::new(),
            },
        ];
        let mut headers = AHashMap::new();
        headers.insert("x-api-key".to_string(), "key-1".to_string());

        let ctx = authenticate(&headers, &backends).unwrap();
        assert_eq!(
            (ctx.backend.as_str(), ctx.backend_index),
            ("api_key", Some(1))
        );
    }

    #[test]
    fn contexts_made_outside_a_backend_list_have_no_position() {
        assert_eq!(
            AuthContext::from_api_key("k", &HashMap::new()).backend_index,
            None
        );
    }

    /// A JWKS that refreshes from a Python function. The function records
    /// each call in `calls` and returns no new document.
    fn refreshing_jwks<'py>(
        py: Python<'py>,
        kids: &[&str],
        refresh_interval: Duration,
    ) -> (Arc<RefreshingJwks>, Bound<'py, PyAny>) {
        slow_refreshing_jwks(py, kids, refresh_interval, 0.0)
    }

    /// A `RefreshingJwks` whose fetch takes `delay` seconds.
    fn slow_refreshing_jwks<'py>(
        py: Python<'py>,
        kids: &[&str],
        refresh_interval: Duration,
        delay: f64,
    ) -> (Arc<RefreshingJwks>, Bound<'py, PyAny>) {
        // from_code reuses a module of the same name, and tests run on
        // parallel threads. Each call gets its own module, so its own calls.
        static MODULES: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let name = format!(
            "refreshing_jwks_{}",
            MODULES.fetch_add(1, Ordering::Relaxed)
        );
        let code = format!(
            "import time\ncalls = []\ndef refresh():\n    time.sleep({delay})\n    calls.append(1)\n"
        );
        let module = PyModule::from_code(
            py,
            &std::ffi::CString::new(code).unwrap(),
            &std::ffi::CString::new(format!("{name}.py")).unwrap(),
            &std::ffi::CString::new(name).unwrap(),
        )
        .unwrap();
        let keys = kids
            .iter()
            .map(|kid| (kid.to_string(), DecodingKey::from_secret(SECRET.as_bytes())))
            .collect();
        let refresh = module.getattr("refresh").unwrap().unbind();
        let jwks = Arc::new(RefreshingJwks::new(keys, refresh, refresh_interval));
        (jwks, module.getattr("calls").unwrap())
    }

    /// Wait, with the GIL released, until the background refresh ends.
    fn wait_for_refresh(py: Python<'_>, jwks: &RefreshingJwks) {
        py.detach(|| {
            let deadline = Instant::now() + Duration::from_secs(5);
            while jwks.refreshing.load(Ordering::Acquire) {
                assert!(
                    Instant::now() < deadline,
                    "the background refresh did not end"
                );
                std::thread::sleep(Duration::from_millis(5));
            }
        });
    }

    #[test]
    fn a_refreshing_jwks_with_one_key_selects_it_for_a_token_with_no_kid() {
        Python::initialize();
        Python::attach(|py| {
            let hour = Duration::from_secs(3600);
            let (one, _) = refreshing_jwks(py, &["a"], hour);
            assert!(one.select(None).is_some());
            // With two keys, a token with no kid cannot choose one.
            let (two, _) = refreshing_jwks(py, &["a", "b"], hour);
            assert!(two.select(None).is_none());
        });
    }

    #[test]
    fn a_refreshing_jwks_fetches_again_only_when_the_interval_ends() {
        Python::initialize();
        Python::attach(|py| {
            let (fresh, calls) = refreshing_jwks(py, &["a"], Duration::from_secs(3600));
            assert!(fresh.select(Some("a")).is_some());
            assert!(fresh.select(Some("a")).is_some());
            assert_eq!(calls.len().unwrap(), 0);

            let (due, calls) = refreshing_jwks(py, &["a"], Duration::ZERO);
            // The fetch gives no new document, so the known key stays.
            assert!(due.select(Some("a")).is_some());
            wait_for_refresh(py, &due);
            assert_eq!(calls.len().unwrap(), 1);
        });
    }

    #[test]
    fn a_due_refresh_runs_in_the_background_and_keeps_the_cached_key() {
        Python::initialize();
        Python::attach(|py| {
            let (jwks, calls) = slow_refreshing_jwks(py, &["a"], Duration::ZERO, 0.5);
            // The request thread holds the GIL, so a fetch on this thread
            // would finish before select returns. The fetch waits instead.
            let started = Instant::now();
            assert!(jwks.select(Some("a")).is_some());
            // A second request while the fetch runs starts no other fetch.
            assert!(jwks.select(Some("a")).is_some());
            assert!(started.elapsed() < Duration::from_millis(400));
            assert_eq!(calls.len().unwrap(), 0);
            wait_for_refresh(py, &jwks);
            assert_eq!(calls.len().unwrap(), 1);
        });
    }

    #[test]
    fn an_unknown_kid_fetches_at_once_and_then_waits_for_the_cooldown() {
        Python::initialize();
        Python::attach(|py| {
            let (jwks, calls) = refreshing_jwks(py, &["a"], Duration::from_secs(3600));
            // A key rotation: the first token with a new kid fetches the JWKS at once.
            assert!(jwks.select(Some("new")).is_none());
            assert_eq!(calls.len().unwrap(), 1);
            // A flood of made-up kids does not fetch again within the cooldown.
            for kid in ["x", "y", "new"] {
                assert!(jwks.select(Some(kid)).is_none());
            }
            assert!(jwks.select(None).is_some());
            assert_eq!(calls.len().unwrap(), 1);
            // A known kid still verifies.
            assert!(jwks.select(Some("a")).is_some());
        });
    }

    #[test]
    fn an_unknown_kid_that_meets_a_running_fetch_does_not_start_the_cooldown() {
        Python::initialize();
        Python::attach(|py| {
            let (jwks, calls) = refreshing_jwks(py, &["a"], Duration::from_secs(3600));
            // Another worker fetches. This request does not wait for it.
            jwks.refreshing.store(true, Ordering::Release);
            assert!(jwks.select(Some("new")).is_none());
            jwks.refreshing.store(false, Ordering::Release);
            assert_eq!(calls.len().unwrap(), 0);
            // The next unknown kid fetches at once.
            assert!(jwks.select(Some("new")).is_none());
            assert_eq!(calls.len().unwrap(), 1);
        });
    }

    #[test]
    fn an_unknown_kid_waits_for_the_cooldown_also_when_the_interval_ends() {
        Python::initialize();
        Python::attach(|py| {
            let (jwks, calls) = refreshing_jwks(py, &["a"], Duration::ZERO);
            assert!(jwks.select(Some("new")).is_none());
            assert_eq!(calls.len().unwrap(), 1);
            // The interval ends at each request, but made-up kids still wait.
            for kid in ["x", "y", "new"] {
                assert!(jwks.select(Some(kid)).is_none());
            }
            assert_eq!(calls.len().unwrap(), 1);
            // A known kid starts the periodic refresh.
            assert!(jwks.select(Some("a")).is_some());
            wait_for_refresh(py, &jwks);
            assert_eq!(calls.len().unwrap(), 2);
        });
    }

    #[test]
    fn extra_claims_keep_their_json_types() {
        Python::initialize();
        let claims: Claims = serde_json::from_value(serde_json::json!({
            "sub": "1",
            "org": 7,
            "ratio": 0.5,
            "beta": true,
            "team": "core",
            "tags": ["x"],
        }))
        .unwrap();
        Python::attach(|py| {
            let dict = claims_to_dict(py, &claims);
            let item = |key: &str| dict.get_item(key).unwrap().unwrap();
            assert_eq!(item("org").extract::<i64>().unwrap(), 7);
            assert_eq!(item("ratio").extract::<f64>().unwrap(), 0.5);
            assert!(item("beta").extract::<bool>().unwrap());
            assert_eq!(item("team").extract::<String>().unwrap(), "core");
            // A list or an object is not converted.
            assert!(item("tags").is_none());
        });
    }

    #[test]
    fn the_context_has_permissions_only_when_there_are_some() {
        Python::initialize();
        let key_permissions = HashMap::from([("k".to_string(), vec!["read".to_string()])]);
        Python::attach(|py| {
            for (key, expected) in [("k", Some(vec!["read".to_string()])), ("other", None)] {
                let context = PyDict::new(py).unbind();
                let auth_ctx = AuthContext::from_api_key(key, &key_permissions);
                populate_auth_context(&context, &auth_ctx, py);
                let permissions = context.bind(py).get_item("permissions").unwrap();
                assert_eq!(
                    permissions.map(|value| value.extract::<Vec<String>>().unwrap()),
                    expected
                );
            }
        });
    }
}
