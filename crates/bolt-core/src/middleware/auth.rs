use ahash::AHashMap;
use aws_lc_rs::{constant_time, hmac};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use jsonwebtoken::{Algorithm, AlgorithmFamily, DecodingKey, crypto};
use pyo3::IntoPyObjectExt;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use serde::Deserialize;
use serde::de::{self, DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde_json::Number;
use std::borrow::Cow;
use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::hash::{BuildHasher, RandomState};
use std::marker::PhantomData;
use std::ops::Range;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use parking_lot::{Mutex, RwLock};
use std::str::FromStr;

/// JWT `aud` claim: RFC 7519 §4.1.3 allows a single string or an array of
/// strings. Real-world providers (e.g. Auth0) emit both forms.
#[derive(Debug, Clone)]
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

/// A claim that [`Claims`] types. Each has one bit in a [`ClaimSet`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TypedClaim {
    Sub,
    Jti,
    Typ,
    Exp,
    Nbf,
    Iat,
    Iss,
    Aud,
    Fam,
    Oat,
    Ver,
    Amr,
    IsStaff,
    IsSuperuser,
    IsAdmin,
    Permissions,
}

impl TypedClaim {
    const ALL: [TypedClaim; 16] = [
        Self::Sub,
        Self::Jti,
        Self::Typ,
        Self::Exp,
        Self::Nbf,
        Self::Iat,
        Self::Iss,
        Self::Aud,
        Self::Fam,
        Self::Oat,
        Self::Ver,
        Self::Amr,
        Self::IsStaff,
        Self::IsSuperuser,
        Self::IsAdmin,
        Self::Permissions,
    ];

    pub fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "sub" => Self::Sub,
            "jti" => Self::Jti,
            "typ" => Self::Typ,
            "exp" => Self::Exp,
            "nbf" => Self::Nbf,
            "iat" => Self::Iat,
            "iss" => Self::Iss,
            "aud" => Self::Aud,
            "fam" => Self::Fam,
            "oat" => Self::Oat,
            "ver" => Self::Ver,
            "amr" => Self::Amr,
            "is_staff" => Self::IsStaff,
            "is_superuser" => Self::IsSuperuser,
            "is_admin" => Self::IsAdmin,
            "permissions" => Self::Permissions,
            _ => return None,
        })
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Sub => "sub",
            Self::Jti => "jti",
            Self::Typ => "typ",
            Self::Exp => "exp",
            Self::Nbf => "nbf",
            Self::Iat => "iat",
            Self::Iss => "iss",
            Self::Aud => "aud",
            Self::Fam => "fam",
            Self::Oat => "oat",
            Self::Ver => "ver",
            Self::Amr => "amr",
            Self::IsStaff => "is_staff",
            Self::IsSuperuser => "is_superuser",
            Self::IsAdmin => "is_admin",
            Self::Permissions => "permissions",
        }
    }

    /// Bolt checks this claim on each token, so a token with it in another
    /// JSON form fails (see [`parse_token_claims`]).
    fn checked_on_each_token(self) -> bool {
        matches!(
            self,
            Self::Sub | Self::Jti | Self::Typ | Self::Exp | Self::Nbf | Self::Iat
        )
    }

    fn bit(self) -> u16 {
        1 << self as u16
    }
}

/// A set of typed claims, one bit for each.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ClaimSet(u16);

impl ClaimSet {
    pub fn contains(self, claim: TypedClaim) -> bool {
        self.0 & claim.bit() != 0
    }

    pub fn is_empty(self) -> bool {
        self.0 == 0
    }

    /// Add `claim`. False when the set has it already.
    pub fn insert(&mut self, claim: TypedClaim) -> bool {
        let added = !self.contains(claim);
        self.0 |= claim.bit();
        added
    }

    pub fn iter(self) -> impl Iterator<Item = TypedClaim> {
        TypedClaim::ALL
            .into_iter()
            .filter(move |claim| self.contains(*claim))
    }
}

/// The claims of a token. A claim has three states: missing, valid and
/// malformed. A typed field holds its claim only in the exact JSON form of
/// its type. In another form, also `null`, the field is `None` and
/// `malformed` has the claim. Each reader of a malformed claim fails: a
/// guard refuses, and `auth_claims` gives `None`. A token with a malformed
/// claim fails only for a claim that Bolt checks (see [`parse_token_claims`]).
#[derive(Debug, Clone, Default)]
pub struct Claims {
    pub sub: Option<String>,                       // Subject (user ID)
    pub exp: Option<Number>,                       // Expiry time (NumericDate)
    pub iat: Option<Number>,                       // Issued at (NumericDate)
    pub nbf: Option<Number>,                       // Not before (NumericDate)
    pub aud: Option<Audience>,                     // Audience (string or array)
    pub iss: Option<String>,                       // Issuer
    pub jti: Option<String>,                       // JWT ID
    pub typ: Option<String>,                       // Token type ("access"/"refresh")
    pub fam: Option<String>,                       // Refresh-token rotation family
    pub oat: Option<i64>,                          // Origin authentication time
    pub ver: Option<i64>,                          // User token version
    pub amr: Option<Vec<String>>,                  // Authentication methods
    pub is_staff: Option<bool>,                    // Staff status
    pub is_superuser: Option<bool>,                // Admin/superuser status
    pub is_admin: Option<bool>,                    // Alternative admin field
    pub permissions: Option<Vec<String>>,          // List of permissions
    pub extra: HashMap<String, serde_json::Value>, // Any extra claims
    /// The typed claims that the token gives in another JSON form.
    pub malformed: ClaimSet,
}

/// The exact JSON form of a typed claim. A value of another JSON type gives
/// `None`: the claim is then malformed.
trait ExactForm: Sized {
    fn from_bool(_: bool) -> Option<Self> {
        None
    }
    fn from_i64(_: i64) -> Option<Self> {
        None
    }
    fn from_u64(value: u64) -> Option<Self> {
        i64::try_from(value).ok().and_then(Self::from_i64)
    }
    fn from_f64(_: f64) -> Option<Self> {
        None
    }
    fn from_str(_: &str) -> Option<Self> {
        None
    }
    fn from_seq<'de, A: SeqAccess<'de>>(mut seq: A) -> Result<Option<Self>, A::Error> {
        while seq.next_element::<IgnoredAny>()?.is_some() {}
        Ok(None)
    }
}

impl ExactForm for bool {
    fn from_bool(value: bool) -> Option<Self> {
        Some(value)
    }
}

impl ExactForm for i64 {
    fn from_i64(value: i64) -> Option<Self> {
        Some(value)
    }
    fn from_f64(value: f64) -> Option<Self> {
        whole_i64(value)
    }
}

/// `value` as an `i64` when it is a whole number in the range of `i64`.
/// JSON does not tell `1` from `1.0` or `1e0` (RFC 8259, section 6).
fn whole_i64(value: f64) -> Option<i64> {
    // `i64::MIN as f64` is exact, and `i64::MAX as f64` is 2^63.
    (value.fract() == 0.0 && value >= i64::MIN as f64 && value < i64::MAX as f64)
        .then_some(value as i64)
}

impl ExactForm for String {
    fn from_str(value: &str) -> Option<Self> {
        Some(value.to_owned())
    }
}

/// `value` as a JSON number. A whole number in the range of `i64`, also as
/// `N.0` or `Ne0`, becomes an integer. Thus it equals `N`, as in Python.
/// None for NaN and the infinities.
pub fn json_number(value: f64) -> Option<Number> {
    whole_i64(value).map_or_else(|| Number::from_f64(value), |whole| Some(whole.into()))
}

/// The NumericDates that a Python `datetime` in UTC can hold: the years 1 to
/// 9999. Python reads the time claims, and a revocation store keeps an entry
/// until a `datetime`. A time out of this range is not a valid time claim.
const NUMERIC_DATE_RANGE: Range<f64> = -62_135_596_800.0..253_402_300_800.0;

/// A NumericDate (RFC 7519, section 2): a JSON number, also with a fraction,
/// in [`NUMERIC_DATE_RANGE`]. A whole number becomes an integer, see
/// [`json_number`]. Thus a guard and Python see the same value as for `N`.
impl ExactForm for Number {
    fn from_i64(value: i64) -> Option<Self> {
        NUMERIC_DATE_RANGE
            .contains(&(value as f64))
            .then(|| value.into())
    }
    fn from_f64(value: f64) -> Option<Self> {
        NUMERIC_DATE_RANGE
            .contains(&value)
            .then(|| json_number(value))?
    }
}

/// An array of strings. An array with an item of another type gives `None`.
impl ExactForm for Vec<String> {
    fn from_seq<'de, A: SeqAccess<'de>>(mut seq: A) -> Result<Option<Self>, A::Error> {
        let mut items = Some(Vec::new());
        while let Some(item) = seq.next_element_seed(Lenient::<String>::default())? {
            match (item, items.as_mut()) {
                (Some(item), Some(items)) => items.push(item),
                (None, _) => items = None,
                (Some(_), None) => {}
            }
        }
        Ok(items)
    }
}

impl ExactForm for Audience {
    fn from_str(value: &str) -> Option<Self> {
        Some(Audience::Single(value.to_owned()))
    }
    fn from_seq<'de, A: SeqAccess<'de>>(seq: A) -> Result<Option<Self>, A::Error> {
        Ok(Vec::<String>::from_seq(seq)?.map(Audience::Multiple))
    }
}

/// Reads a JSON value of any type, and gives it only in the exact form `T`.
struct Lenient<T>(PhantomData<T>);

impl<T> Default for Lenient<T> {
    fn default() -> Self {
        Self(PhantomData)
    }
}

impl<'de, T: ExactForm> DeserializeSeed<'de> for Lenient<T> {
    type Value = Option<T>;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Option<T>, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de, T: ExactForm> Visitor<'de> for Lenient<T> {
    type Value = Option<T>;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("any JSON value")
    }
    fn visit_bool<E>(self, value: bool) -> Result<Option<T>, E> {
        Ok(T::from_bool(value))
    }
    fn visit_i64<E>(self, value: i64) -> Result<Option<T>, E> {
        Ok(T::from_i64(value))
    }
    fn visit_u64<E>(self, value: u64) -> Result<Option<T>, E> {
        Ok(T::from_u64(value))
    }
    fn visit_f64<E>(self, value: f64) -> Result<Option<T>, E> {
        Ok(T::from_f64(value))
    }
    fn visit_str<E>(self, value: &str) -> Result<Option<T>, E> {
        Ok(T::from_str(value))
    }
    fn visit_unit<E>(self) -> Result<Option<T>, E> {
        Ok(None)
    }
    fn visit_none<E>(self) -> Result<Option<T>, E> {
        Ok(None)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, seq: A) -> Result<Option<T>, A::Error> {
        T::from_seq(seq)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Option<T>, A::Error> {
        while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
        Ok(None)
    }
}

/// An object key. It borrows from the input when it has no escape.
struct Key<'de>(Cow<'de, str>);

impl<'de> Deserialize<'de> for Key<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct KeyVisitor;
        impl<'de> Visitor<'de> for KeyVisitor {
            type Value = Key<'de>;
            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a string key")
            }
            fn visit_borrowed_str<E>(self, value: &'de str) -> Result<Key<'de>, E> {
                Ok(Key(Cow::Borrowed(value)))
            }
            fn visit_str<E>(self, value: &str) -> Result<Key<'de>, E> {
                Ok(Key(Cow::Owned(value.to_owned())))
            }
            fn visit_string<E>(self, value: String) -> Result<Key<'de>, E> {
                Ok(Key(Cow::Owned(value)))
            }
        }
        deserializer.deserialize_str(KeyVisitor)
    }
}

fn duplicate_name<E: de::Error>(name: &str) -> E {
    E::custom(format_args!("duplicate name `{name}`"))
}

/// Reads a JSON object of claims in one pass.
///
/// A name that occurs two times is an error (RFC 7519, section 4 allows it).
/// A typed claim in another JSON type, also `null`, is malformed. With
/// `strict`, a malformed claim that Bolt checks on each token is an error:
/// `sub` and `jti` (strings, as PyJWT requires), `typ` (a string), and `exp`,
/// `nbf` and `iat` (numbers).
struct ClaimsVisitor {
    strict: bool,
}

/// Read the next value into `field`. False when the value is malformed.
fn read_typed<'de, T: ExactForm, A: MapAccess<'de>>(
    map: &mut A,
    field: &mut Option<T>,
) -> Result<bool, A::Error> {
    *field = map.next_value_seed(Lenient::default())?;
    Ok(field.is_some())
}

impl<'de> Visitor<'de> for ClaimsVisitor {
    type Value = Claims;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON object of claims")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Claims, A::Error> {
        let mut claims = Claims::default();
        let mut seen = ClaimSet::default();
        while let Some(Key(name)) = map.next_key()? {
            let Some(claim) = TypedClaim::from_name(&name) else {
                let value = map.next_value()?;
                match claims.extra.entry(name.into_owned()) {
                    Entry::Occupied(entry) => return Err(duplicate_name(entry.key())),
                    Entry::Vacant(entry) => entry.insert(value),
                };
                continue;
            };
            if !seen.insert(claim) {
                return Err(duplicate_name(&name));
            }
            let valid = match claim {
                TypedClaim::Sub => read_typed(&mut map, &mut claims.sub)?,
                TypedClaim::Jti => read_typed(&mut map, &mut claims.jti)?,
                TypedClaim::Typ => read_typed(&mut map, &mut claims.typ)?,
                TypedClaim::Exp => read_typed(&mut map, &mut claims.exp)?,
                TypedClaim::Nbf => read_typed(&mut map, &mut claims.nbf)?,
                TypedClaim::Iat => read_typed(&mut map, &mut claims.iat)?,
                TypedClaim::Iss => read_typed(&mut map, &mut claims.iss)?,
                TypedClaim::Aud => read_typed(&mut map, &mut claims.aud)?,
                TypedClaim::Fam => read_typed(&mut map, &mut claims.fam)?,
                TypedClaim::Oat => read_typed(&mut map, &mut claims.oat)?,
                TypedClaim::Ver => read_typed(&mut map, &mut claims.ver)?,
                TypedClaim::Amr => read_typed(&mut map, &mut claims.amr)?,
                TypedClaim::IsStaff => read_typed(&mut map, &mut claims.is_staff)?,
                TypedClaim::IsSuperuser => read_typed(&mut map, &mut claims.is_superuser)?,
                TypedClaim::IsAdmin => read_typed(&mut map, &mut claims.is_admin)?,
                TypedClaim::Permissions => read_typed(&mut map, &mut claims.permissions)?,
            };
            if !valid {
                if self.strict && claim.checked_on_each_token() {
                    return Err(de::Error::custom(format_args!(
                        "claim `{name}` has the wrong JSON type"
                    )));
                }
                claims.malformed.insert(claim);
            }
        }
        Ok(claims)
    }
}

/// Lenient: each typed claim of another JSON type is malformed, with no
/// error. The MCP bridge reads the claims of a verified principal this way.
impl<'de> Deserialize<'de> for Claims {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(ClaimsVisitor { strict: false })
    }
}

/// A part of a token as text. Each part is UTF-8 (RFC 7515, section 5.2).
/// serde_json does not check the UTF-8 of a value that it skips in a slice.
fn token_text(bytes: &[u8]) -> serde_json::Result<&str> {
    std::str::from_utf8(bytes).map_err(de::Error::custom)
}

/// Parse the claims of a token. A claim that Bolt checks on each token must
/// have its JSON type, see [`ClaimsVisitor`].
fn parse_token_claims(bytes: &[u8]) -> serde_json::Result<Claims> {
    let mut deserializer = serde_json::Deserializer::from_str(token_text(bytes)?);
    let claims = deserializer.deserialize_map(ClaimsVisitor { strict: true })?;
    deserializer.end()?;
    Ok(claims)
}

/// Authentication context built from successful authentication
#[derive(Debug, Clone)]
pub struct AuthContext {
    pub user_id: Option<String>,
    pub is_staff: bool,
    pub is_superuser: bool,
    /// The scheme name of the backend: "jwt", "api_key" or "session". A
    /// principal from an MCP `verify_token` hook brings its own name.
    pub backend: Cow<'static, str>,
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
    pub fn from_jwt_claims(mut claims: Claims, backend: &'static str, cookie_csrf: bool) -> Self {
        let user_id = claims.sub.clone();
        let is_staff = claims.is_staff.unwrap_or(false);
        let is_superuser = claims.is_superuser.unwrap_or(false);

        // The guards and Python read the permissions from the context, not
        // from the claims, so they move out of the claims.
        let permissions: HashSet<String> =
            claims.permissions.take().into_iter().flatten().collect();

        AuthContext {
            user_id,
            is_staff,
            is_superuser,
            backend: Cow::Borrowed(backend),
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
            backend: Cow::Borrowed("api_key"),
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
            backend: Cow::Borrowed("session"),
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
    /// fetch keeps the last known-good keys and logs a warning.
    fn fetch(&self) {
        let keys = Python::attach(|py| {
            let document = match self
                .refresh
                .call0(py)
                .and_then(|value| value.extract::<Option<String>>(py))
            {
                Ok(Some(document)) => document,
                // The callback logged its own failure, or a refresh already runs.
                Ok(None) => return None,
                Err(err) => {
                    log_jwks_refresh_failure(py, &format!("the refresh callback raised {err}"));
                    return None;
                }
            };
            match build_jwks_key_source(&document) {
                Ok(JwtKeySource::Jwks(keys)) => Some(keys),
                Ok(_) => None,
                Err(err) => {
                    log_jwks_refresh_failure(py, &err);
                    None
                }
            }
        });
        *self.last_refresh.lock() = Instant::now();
        if let Some(keys) = keys {
            *self.keys.write() = keys;
        }
    }
}

/// Log a JWKS refresh that Bolt cannot use to the `django_bolt.auth` logger.
///
/// Bolt keeps the last keys, so a token from a new key gets a 401. The log
/// is the only sign that the provider fails. The `log` crate has no output here.
fn log_jwks_refresh_failure(py: Python<'_>, reason: &str) {
    let message = format!("JWKS refresh failed: {reason}. Bolt keeps the last keys.");
    let logged = py
        .import(intern!(py, "logging"))
        .and_then(|logging| logging.call_method1(intern!(py, "getLogger"), ("django_bolt.auth",)))
        .and_then(|logger| logger.call_method1(intern!(py, "warning"), (message.as_str(),)));
    if let Err(err) = logged {
        eprintln!("[django-bolt] {message} Logging failed: {err}");
    }
}

/// The valid keys of an API key backend, by their SipHash.
///
/// The SipHash key is random for each `ApiKeys`, so a client cannot compute
/// the hash of a key. `find` looks up the hash of the request key, and then
/// compares the request key in constant time with each valid key of that
/// hash. Thus only the constant-time comparison reads the bytes of a valid
/// key, and the cost does not grow with the number of keys.
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
/// string.
///
/// A member that Bolt cannot use is skipped, as RFC 7517, section 5 asks:
/// a key of an unknown type, a key with a missing or bad member, and a value
/// that is not a JWK. A key with no `kid` is skipped, because no token can
/// select it. A key whose `use` is not "sig" is skipped (RFC 7517, section
/// 4.2), so an encryption key never verifies a token. A document with no
/// usable key is an error.
pub fn build_jwks_key_source(jwks_json: &str) -> Result<JwtKeySource, String> {
    #[derive(serde::Deserialize)]
    struct Members {
        keys: Vec<serde_json::Value>,
    }
    let set: Members = serde_json::from_str(jwks_json)
        .map_err(|e| format!("Invalid JWKS document for JWT auth: {}", e))?;
    let mut keys = HashMap::new();
    for member in set.keys {
        let Ok(jwk) = serde_json::from_value::<jsonwebtoken::jwk::Jwk>(member) else {
            continue;
        };
        let Some(kid) = jwk.common.key_id.clone() else {
            continue;
        };
        if jwk
            .common
            .public_key_use
            .as_ref()
            .is_some_and(|key_use| *key_use != jsonwebtoken::jwk::PublicKeyUse::Signature)
        {
            continue;
        }
        let Ok(key) = DecodingKey::from_jwk(&jwk) else {
            continue;
        };
        keys.insert(kid, key);
    }
    if keys.is_empty() {
        return Err(
            "JWKS document contained no usable keys (each key needs a 'kid', a type that Bolt supports, and no 'use' other than 'sig')"
                .to_string(),
        );
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

/// Minimal JOSE header: only the fields the validation loop acts on.
///
/// Deliberately NOT `jsonwebtoken::Header`: its `extras` map is typed
/// `HashMap<String, String>`, so any non-string extension parameter —
/// which RFC 7515 §4.1.11 explicitly permits, and providers emit (Clerk:
/// `"oiat": <int>`, Auth0: `"gty": [...]`) — fails to parse before
/// signature verification even runs. Other parameters are ignored
/// regardless of their JSON type.
#[derive(Default)]
struct TokenHeader {
    alg: String,
    /// A string. `null` or another type is an error, as in PyJWT.
    kid: Option<String>,
    /// The header has `crit`. Bolt supports no critical extension, so each
    /// such token is invalid (RFC 7515, section 4.1.11).
    crit: bool,
    /// `"b64": false`: the payload is not base64url (RFC 7797). Bolt does
    /// not support this form, and PyJWT refuses it with no detached payload.
    unencoded_payload: bool,
}

/// Reads a JOSE header: a JSON object, not an array. A parameter that Bolt
/// reads must occur one time.
impl<'de> Deserialize<'de> for TokenHeader {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct HeaderVisitor;
        impl<'de> Visitor<'de> for HeaderVisitor {
            type Value = TokenHeader;
            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<TokenHeader, A::Error> {
                let mut header = TokenHeader::default();
                let mut alg = None;
                let mut seen = 0u8;
                while let Some(Key(name)) = map.next_key()? {
                    let bit = match name.as_ref() {
                        "alg" => {
                            alg = Some(map.next_value::<String>()?);
                            1
                        }
                        "kid" => {
                            header.kid = Some(map.next_value::<String>()?);
                            1 << 1
                        }
                        "crit" => {
                            map.next_value::<IgnoredAny>()?;
                            header.crit = true;
                            1 << 2
                        }
                        "b64" => {
                            header.unencoded_payload =
                                map.next_value_seed(Lenient::<bool>::default())? == Some(false);
                            1 << 3
                        }
                        _ => {
                            map.next_value::<IgnoredAny>()?;
                            continue;
                        }
                    };
                    if seen & bit != 0 {
                        return Err(duplicate_name(&name));
                    }
                    seen |= bit;
                }
                header.alg = alg.ok_or_else(|| de::Error::missing_field("alg"))?;
                Ok(header)
            }
        }
        deserializer.deserialize_map(HeaderVisitor)
    }
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

/// The token types that the claims declare: `typ`, then each value of
/// [`OTHER_TOKEN_TYPE_CLAIMS`]. None when one of them is not a string, also
/// `null`: Bolt cannot read the type of such a token.
fn declared_token_types(claims: &Claims) -> Option<[Option<&str>; 3]> {
    let mut types = [claims.typ.as_deref(), None, None];
    for (slot, name) in types[1..].iter_mut().zip(OTHER_TOKEN_TYPE_CLAIMS) {
        *slot = match claims.extra.get(name) {
            None => None,
            Some(serde_json::Value::String(value)) => Some(value.as_str()),
            Some(_) => return None,
        };
    }
    Some(types)
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |since| since.as_secs_f64())
}

/// Check `exp`, `nbf` and `iat` at `now`, with `leeway` seconds of clock skew.
///
/// The fraction of `exp` is removed, as `int()` of PyJWT does: a token never
/// lives longer than PyJWT allows. `nbf` and `iat` are not rounded: a token
/// counts only from `leeway` seconds before the time that it states (RFC
/// 7519, section 4.1.5). Each test is the condition to accept, so a NaN refuses.
fn check_time_claims(claims: &Claims, now: f64, leeway: f64) -> Result<(), &'static str> {
    let live = claims
        .exp
        .as_ref()
        .and_then(Number::as_f64)
        .map(|exp| exp.trunc() > now - leeway);
    match live {
        None => return Err("missing required exp claim"),
        Some(false) => return Err("token expired"),
        Some(true) => {}
    }
    let not_after = |claim: &Option<Number>| {
        claim
            .as_ref()
            .is_none_or(|time| time.as_f64().is_some_and(|time| time <= now + leeway))
    };
    if !not_after(&claims.nbf) {
        return Err("token not yet valid (nbf)");
    }
    if !not_after(&claims.iat) {
        return Err("token issued in the future (iat)");
    }
    Ok(())
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
    let header: TokenHeader = match token_text(&header_bytes).and_then(serde_json::from_str) {
        Ok(header) => header,
        Err(e) => {
            log::debug!("JWT rejected: header failed to parse: {}", e);
            return None;
        }
    };
    if header.crit {
        log::debug!("JWT rejected: the header has crit, and Bolt supports no extension");
        return None;
    }
    if header.unencoded_payload {
        log::debug!("JWT rejected: the header has \"b64\": false");
        return None;
    }

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
    let claims = match parse_token_claims(&claims_bytes) {
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
    let Some(declared) = declared_token_types(&claims) else {
        log::debug!("JWT rejected: a token type claim is not a string");
        return None;
    };
    match token_type {
        Some(expected) => {
            if claims.typ.as_deref() != Some(expected)
                || declared.into_iter().flatten().any(|t| t != expected)
            {
                log::debug!("JWT rejected: token type claims do not match expected token type");
                return None;
            }
        }
        None => {
            if declared.into_iter().flatten().any(is_non_access_token_type) {
                log::debug!("JWT rejected: refresh or ID token used on an access route");
                return None;
            }
        }
    }

    if let Err(reason) = check_time_claims(&claims, now_seconds(), leeway as f64) {
        log::debug!("JWT rejected: {}", reason);
        return None;
    }
    // aud is only validated when an audience is configured; a token merely
    // carrying an aud claim is not rejected (unlike jsonwebtoken's default).
    // An aud that is not a string or an array of strings is None here.
    if let Some(expected) = audience {
        match &claims.aud {
            Some(aud) if aud.contains(expected) => {}
            _ => {
                log::debug!("JWT rejected: audience mismatch");
                return None;
            }
        }
    }
    // When an issuer is configured, a token without a string iss is rejected.
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

    // Remove the "Bearer" scheme if present
    let token = strip_scheme(&raw_token, "Bearer").unwrap_or(&raw_token);

    let claims = decode_and_validate(
        token, keys, algorithms, audience, issuer, leeway, token_type,
    )?;
    if require_jti && claims.jti.as_deref().is_none_or(str::is_empty) {
        log::debug!("JWT rejected: missing required jti claim");
        return None;
    }
    Some(AuthContext::from_jwt_claims(claims, "jwt", cookie_csrf))
}

/// Remove an auth scheme and the spaces after it from a header value.
///
/// The name of an auth scheme is case-insensitive (RFC 9110 section 11.1).
/// One or more spaces end it (`1*SP` in RFC 9110 section 11.4). A tab is
/// not a space there. `scheme` is ASCII. The comparison reads bytes and
/// does not allocate.
#[inline]
pub fn strip_scheme<'a>(value: &'a str, scheme: &str) -> Option<&'a str> {
    let bytes = value.as_bytes();
    let len = scheme.len();
    if bytes.len() > len
        && bytes[len] == b' '
        && bytes[..len].eq_ignore_ascii_case(scheme.as_bytes())
    {
        // The bytes before `len + 1` are ASCII, so this is a char boundary.
        Some(value[len + 1..].trim_start_matches(' '))
    } else {
        None
    }
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

    // Extract key (remove the "Bearer" or "ApiKey" scheme if present)
    let api_key = strip_scheme(api_key_header, "Bearer")
        .or_else(|| strip_scheme(api_key_header, "ApiKey"))
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
    let backend = match auth_ctx.backend.as_ref() {
        "jwt" => intern!(py, "jwt"),
        "api_key" => intern!(py, "api_key"),
        "session" => intern!(py, "session"),
        other => &PyString::new(py, other),
    };
    let _ = dict.set_item(intern!(py, "auth_backend"), backend);
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
/// plus every extra claim from the JWT payload. A malformed typed claim has
/// the value `None`, so Python can tell it from a missing claim.
fn claims_to_dict<'py>(py: Python<'py>, claims: &Claims) -> Bound<'py, PyDict> {
    let claims_dict = PyDict::new(py);

    set_if_some!(claims_dict, py, "sub", &claims.sub);
    set_if_some!(
        claims_dict,
        py,
        "exp",
        claims.exp.as_ref().map(|n| number_to_py(py, n))
    );
    set_if_some!(
        claims_dict,
        py,
        "iat",
        claims.iat.as_ref().map(|n| number_to_py(py, n))
    );
    set_if_some!(claims_dict, py, "is_staff", claims.is_staff);
    set_if_some!(claims_dict, py, "is_superuser", claims.is_superuser);
    set_if_some!(claims_dict, py, "is_admin", claims.is_admin);
    set_if_some!(
        claims_dict,
        py,
        "nbf",
        claims.nbf.as_ref().map(|n| number_to_py(py, n))
    );
    match &claims.aud {
        Some(Audience::Single(aud)) => {
            let _ = claims_dict.set_item(intern!(py, "aud"), aud);
        }
        Some(Audience::Multiple(auds)) => {
            let _ = claims_dict.set_item(intern!(py, "aud"), auds);
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
            serde_json::Value::String(s) => PyString::new(py, s).into_any().unbind(),
            serde_json::Value::Number(n) => number_to_py(py, n),
            serde_json::Value::Bool(b) => (*b).into_py_any(py).unwrap_or_else(|_| py.None()),
            _ => py.None(),
        };
        let _ = claims_dict.set_item(key, py_value);
    }
    if !claims.malformed.is_empty() {
        for claim in claims.malformed.iter() {
            let _ = claims_dict.set_item(claim.name(), py.None());
        }
    }

    claims_dict
}

/// A JSON number as Python's `json.loads` gives it: an int for an integer,
/// a float for a number with a fraction or an exponent.
fn number_to_py(py: Python<'_>, number: &Number) -> Py<PyAny> {
    let value = if let Some(i) = number.as_i64() {
        i.into_py_any(py)
    } else if let Some(u) = number.as_u64() {
        u.into_py_any(py)
    } else if let Some(f) = number.as_f64() {
        f.into_py_any(py)
    } else {
        Ok(py.None())
    };
    value.unwrap_or_else(|_| py.None())
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
    use jsonwebtoken::{EncodingKey, get_current_timestamp};
    use proptest::prelude::*;

    const SECRET: &str = "test-secret";

    fn hs256_key() -> JwtKeySource {
        JwtKeySource::Static(StaticKey::new(DecodingKey::from_secret(SECRET.as_bytes())))
    }

    /// Build a token from raw JSON parts so tests can emit headers that
    /// `jsonwebtoken::Header` cannot represent (non-string extras).
    fn sign_token(header_json: &str, claims_json: &str, alg: Algorithm) -> String {
        sign_bytes(header_json.as_bytes(), claims_json.as_bytes(), alg)
    }

    /// [`sign_token`] for parts that need not be UTF-8.
    fn sign_bytes(header_json: &[u8], claims_json: &[u8], alg: Algorithm) -> String {
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

    /// The kids of the keys that a JWKS document gives.
    fn jwks_kids(document: &str) -> Vec<String> {
        match build_jwks_key_source(document) {
            Ok(JwtKeySource::Jwks(keys)) => {
                let mut kids: Vec<String> = keys.into_keys().collect();
                kids.sort();
                kids
            }
            other => panic!("{other:?}"),
        }
    }

    /// An `oct` key: the smallest JWK that `DecodingKey::from_jwk` accepts.
    const GOOD_JWK: &str = r#"{"kty":"oct","kid":"good","k":"c2VjcmV0"}"#;

    #[test]
    fn a_jwks_member_that_bolt_cannot_use_is_skipped() {
        // RFC 7517, section 5: ignore a key of an unknown type, with a
        // missing member, or with a value out of range.
        for member in [
            r#"{"kty":"OKP","crv":"X25519","x":"hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo","kid":"x"}"#,
            r#"{"kty":"AKP","alg":"ML-DSA-44","pub":"AAAA","kid":"x"}"#,
            r#"{"kty":"RSA","n":"AQAB","kid":"x"}"#,
            r#""not-a-key""#,
            r#"{"kid":"x"}"#,
        ] {
            let document = format!(r#"{{"keys":[{GOOD_JWK},{member}]}}"#);
            assert_eq!(jwks_kids(&document), ["good"], "{member}");
        }
    }

    #[test]
    fn a_jwks_key_for_encryption_does_not_verify() {
        // RFC 7517, section 4.2: "use" "enc" is for encryption.
        let document = format!(
            r#"{{"keys":[{GOOD_JWK},{{"kty":"oct","kid":"enc","use":"enc","k":"c2VjcmV0"}},{{"kty":"oct","kid":"sig","use":"sig","k":"c2VjcmV0"}}]}}"#
        );
        assert_eq!(jwks_kids(&document), ["good", "sig"]);
    }

    #[test]
    fn a_jwks_with_no_usable_key_is_an_error() {
        let document = r#"{"keys":[{"kty":"oct","kid":"enc","use":"enc","k":"c2VjcmV0"},"x"]}"#;
        let error = build_jwks_key_source(document).unwrap_err();
        assert!(error.contains("no usable keys"), "{error}");
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
            (first.backend.as_ref(), first.backend_index),
            ("jwt", Some(0))
        );

        let second = authenticate(&bearer_headers("secret-b"), &backends).unwrap();
        assert_eq!(
            (second.backend.as_ref(), second.backend_index),
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
    fn a_key_with_the_hash_of_a_valid_key_must_still_equal_it() {
        // A random hash state cannot give a collision on demand, so the test
        // puts a valid key in the bucket of the hash of "candidate".
        let mut keys: ApiKeys = ["key-one".to_string()].into_iter().collect();
        let bucket = keys.state.hash_one("candidate");
        keys.by_hash
            .entry(bucket)
            .or_default()
            .push("other-key".to_string());
        assert_eq!(keys.find("candidate"), None);
        assert_eq!(keys.find("other-key"), None);
        assert_eq!(keys.find("key-one"), Some("key-one"));
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
    fn the_auth_scheme_of_a_credential_has_no_case() {
        // RFC 9110 section 11.1: the name of an auth scheme is case-insensitive.
        let token = bearer_headers(SECRET)["authorization"]
            .strip_prefix("Bearer ")
            .unwrap()
            .to_string();
        let jwt = [hs256_backend(SECRET)];
        for scheme in ["Bearer", "bearer", "BEARER", "bEaReR"] {
            let headers =
                AHashMap::from_iter([("authorization".to_string(), format!("{scheme} {token}"))]);
            let ctx = authenticate(&headers, &jwt);
            assert_eq!(ctx.unwrap().user_id.as_deref(), Some("42"), "{scheme}");
        }
        // A space ends the scheme, and another scheme stays in the token.
        for value in [format!("bearer{token}"), format!("basic {token}")] {
            let headers = AHashMap::from_iter([("authorization".to_string(), value.clone())]);
            assert!(authenticate(&headers, &jwt).is_none(), "{value}");
        }

        let api_key = [AuthBackend::APIKey {
            api_keys: ["key-one".to_string()].into_iter().collect(),
            header: "x-api-key".to_string(),
            key_permissions: HashMap::new(),
        }];
        for value in [
            "bearer key-one",
            "BEARER key-one",
            "apikey key-one",
            "APIKEY key-one",
        ] {
            let headers = AHashMap::from_iter([("x-api-key".to_string(), value.to_string())]);
            let ctx = authenticate(&headers, &api_key);
            assert_eq!(
                ctx.unwrap().user_id.as_deref(),
                Some("apikey:key-one"),
                "{value}"
            );
        }
        for value in ["bearerkey-one", "apikeykey-one", "bearér key-one"] {
            let headers = AHashMap::from_iter([("x-api-key".to_string(), value.to_string())]);
            assert!(authenticate(&headers, &api_key).is_none(), "{value}");
        }
    }

    #[test]
    fn strip_scheme_removes_the_scheme_and_its_spaces() {
        assert_eq!(strip_scheme("Bearer abc", "Bearer"), Some("abc"));
        assert_eq!(strip_scheme("bEARER abc", "Bearer"), Some("abc"));
        assert_eq!(strip_scheme("Bearer   abc", "Bearer"), Some("abc"));
        assert_eq!(strip_scheme("Bearer \tabc", "Bearer"), Some("\tabc"));
        assert_eq!(strip_scheme("Bearer\tabc", "Bearer"), None);
        assert_eq!(strip_scheme("Bearer ", "Bearer"), Some(""));
        assert_eq!(strip_scheme("Bearer   ", "Bearer"), Some(""));
        assert_eq!(strip_scheme("Bearer", "Bearer"), None);
        assert_eq!(strip_scheme("Bearerabc", "Bearer"), None);
        assert_eq!(strip_scheme("Bear", "Bearer"), None);
        assert_eq!(strip_scheme("", "Bearer"), None);
        // A multi-byte character at the place of the space is not a char boundary.
        assert_eq!(strip_scheme("Beareré abc", "Bearer"), None);
        assert_eq!(strip_scheme("Bearé abc", "Bearer"), None);
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
            (ctx.backend.as_ref(), ctx.backend_index),
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

    /// The log is the only sign that the provider fails.
    #[test]
    fn a_failed_refresh_logs_a_warning_and_keeps_the_last_keys() {
        Python::initialize();
        Python::attach(|py| {
            let code = "import logging\n\
                records = []\n\
                class Keep(logging.Handler):\n    \
                    def emit(self, record):\n        \
                        records.append((record.levelname, record.getMessage()))\n\
                logging.getLogger('django_bolt.auth').addHandler(Keep())\n\
                def refresh():\n    \
                    raise RuntimeError('provider 7391 is down')\n";
            let module = PyModule::from_code(
                py,
                &std::ffi::CString::new(code).unwrap(),
                c"failing_jwks_refresh.py",
                c"failing_jwks_refresh",
            )
            .unwrap();
            let key = DecodingKey::from_secret(SECRET.as_bytes());
            let keys = HashMap::from([("a".to_string(), key)]);
            let refresh = module.getattr("refresh").unwrap().unbind();
            let jwks = Arc::new(RefreshingJwks::new(
                keys,
                refresh,
                Duration::from_secs(3600),
            ));

            jwks.fetch();

            assert!(jwks.select(Some("a")).is_some());
            let records: Vec<(String, String)> =
                module.getattr("records").unwrap().extract().unwrap();
            let expected = (
                "WARNING".to_string(),
                "JWKS refresh failed: the refresh callback raised RuntimeError: \
                 provider 7391 is down. Bolt keeps the last keys."
                    .to_string(),
            );
            assert!(records.contains(&expected), "{records:?}");
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
    fn the_context_names_the_backend_and_keeps_the_jwt_claims() {
        Python::initialize();
        let claims: Claims = serde_json::from_value(serde_json::json!({
            "sub": "1",
            "aud": ["a", "b"],
            "permissions": ["read", "write"],
        }))
        .unwrap();
        let mut principal = AuthContext::from_api_key("k", &HashMap::new());
        principal.backend = "oauth".to_string().into();
        let contexts = [
            ("jwt", AuthContext::from_jwt_claims(claims, "jwt", false)),
            ("api_key", AuthContext::from_api_key("k", &HashMap::new())),
            ("session", AuthContext::from_session(0, false)),
            // A principal from an MCP `verify_token` hook names its own backend.
            ("oauth", principal),
        ];
        Python::attach(|py| {
            for (name, auth_ctx) in &contexts {
                let context = PyDict::new(py).unbind();
                populate_auth_context(&context, auth_ctx, py);
                let context = context.bind(py);
                let item = |key: &str| context.get_item(key).unwrap();
                assert_eq!(
                    item("auth_backend").unwrap().extract::<String>().unwrap(),
                    *name
                );
                if *name != "jwt" {
                    continue;
                }
                let mut permissions: Vec<String> = item("permissions").unwrap().extract().unwrap();
                permissions.sort();
                assert_eq!(permissions, ["read", "write"]);
                let claims = item("auth_claims").unwrap();
                let claim = |key: &str| claims.get_item(key).unwrap();
                assert_eq!(claim("sub").extract::<String>().unwrap(), "1");
                assert_eq!(claim("aud").extract::<Vec<String>>().unwrap(), ["a", "b"]);
            }
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

    /// Whether a token of these raw header and claims passes an HS256
    /// backend with no audience, no issuer and no leeway.
    fn passes(header_json: &str, claims_json: &str) -> bool {
        validate(
            &sign_token(header_json, claims_json, Algorithm::HS256),
            None,
            0,
        )
        .is_some()
    }

    fn with_exp(claims: &str) -> String {
        let exp = future_exp();
        if claims.is_empty() {
            format!(r#"{{"sub":"42","exp":{exp}}}"#)
        } else {
            format!(r#"{{"sub":"42","exp":{exp},{claims}}}"#)
        }
    }

    /// RFC 7519, section 2: a NumericDate can have a fraction. PyJWT accepts it.
    #[test]
    fn j1_a_fractional_numeric_date_is_accepted() {
        let now = get_current_timestamp() as f64;
        for claims in [
            format!(r#"{{"sub":"42","exp":{}}}"#, now + 3600.5),
            format!(r#"{{"sub":"42","exp":{}e0}}"#, now + 3600.0),
            format!(
                r#"{{"sub":"42","exp":{},"nbf":{}}}"#,
                now + 3600.0,
                now - 100.25
            ),
            format!(
                r#"{{"sub":"42","exp":{},"iat":{}}}"#,
                now + 3600.0,
                now - 100.75
            ),
        ] {
            assert!(passes(r#"{"alg":"HS256"}"#, &claims), "{claims}");
        }
    }

    /// A claim that Bolt does not check does not refuse the token because of
    /// its JSON type. PyJWT accepts each of these tokens.
    #[test]
    fn j2_an_unchecked_claim_of_another_type_does_not_refuse_the_token() {
        for claim in [
            r#""iss":123"#,
            r#""iss":null"#,
            r#""aud":123"#,
            r#""aud":["api",1]"#,
            r#""is_staff":1"#,
            r#""is_superuser":"true""#,
            r#""is_admin":null"#,
            r#""permissions":"read""#,
            r#""permissions":["read",1]"#,
            r#""amr":"pwd""#,
            r#""fam":7"#,
            r#""oat":1.5"#,
            r#""ver":"1""#,
        ] {
            assert!(passes(r#"{"alg":"HS256"}"#, &with_exp(claim)), "{claim}");
        }
    }

    /// RFC 9110, section 11.4: `credentials = auth-scheme [ 1*SP ( token68 / #auth-param ) ]`.
    #[test]
    fn j3_more_than_one_space_after_the_scheme() {
        let token = bearer_headers(SECRET)["authorization"]
            .strip_prefix("Bearer ")
            .unwrap()
            .to_string();
        let jwt = [hs256_backend(SECRET)];
        for value in [format!("Bearer  {token}"), format!("bearer     {token}")] {
            let headers = AHashMap::from_iter([("authorization".to_string(), value.clone())]);
            assert!(authenticate(&headers, &jwt).is_some(), "{value}");
        }
        // A tab or another white space is not a space of RFC 9110.
        for value in [format!("Bearer\t{token}"), format!("Bearer \t{token}")] {
            let headers = AHashMap::from_iter([("authorization".to_string(), value.clone())]);
            assert!(authenticate(&headers, &jwt).is_none(), "{value:?}");
        }

        let api_key = [AuthBackend::APIKey {
            api_keys: ["key-one".to_string()].into_iter().collect(),
            header: "x-api-key".to_string(),
            key_permissions: HashMap::new(),
        }];
        for value in ["Bearer  key-one", "apikey   key-one"] {
            let headers = AHashMap::from_iter([("x-api-key".to_string(), value.to_string())]);
            assert!(authenticate(&headers, &api_key).is_some(), "{value}");
        }
        let headers =
            AHashMap::from_iter([("x-api-key".to_string(), "ApiKey\tkey-one".to_string())]);
        assert!(authenticate(&headers, &api_key).is_none());
    }

    /// JSON null is not a missing claim or header parameter. PyJWT refuses each.
    #[test]
    fn j4_a_null_registered_claim_is_refused() {
        for claim in [
            r#""sub":null"#,
            r#""jti":null"#,
            r#""nbf":null"#,
            r#""iat":null"#,
            r#""typ":null"#,
        ] {
            assert!(!passes(r#"{"alg":"HS256"}"#, &with_exp(claim)), "{claim}");
        }
        assert!(!passes(r#"{"alg":"HS256"}"#, r#"{"sub":"42","exp":null}"#));
        for header in [r#"{"alg":"HS256","kid":null}"#, r#"{"alg":null}"#] {
            assert!(!passes(header, &with_exp("")), "{header}");
        }
    }

    /// PyJWT 2.10 and later refuse a token whose iat is after now plus the leeway.
    #[test]
    fn j5_a_token_issued_in_the_future_is_refused() {
        let now = get_current_timestamp() as i64;
        let issued_at = |iat: i64| {
            let claims = format!(r#"{{"sub":"42","exp":{},"iat":{iat}}}"#, future_exp());
            sign_token(r#"{"alg":"HS256"}"#, &claims, Algorithm::HS256)
        };
        assert!(validate(&issued_at(now + 3600), None, 60).is_none());
        assert!(validate(&issued_at(now + 30), None, 0).is_none());
        // The leeway applies to iat too.
        assert!(validate(&issued_at(now + 30), None, 60).is_some());
        assert!(validate(&issued_at(now - 30), None, 0).is_some());
    }

    /// RFC 7515, section 4.1.11: a token that lists an extension that the
    /// recipient does not support is invalid. Bolt supports none.
    #[test]
    fn j6_a_token_with_a_critical_extension_is_refused() {
        for header in [
            r#"{"alg":"HS256","crit":["exp"],"exp":1}"#,
            r#"{"alg":"HS256","crit":["b64"],"b64":true}"#,
            r#"{"alg":"HS256","crit":[]}"#,
            r#"{"alg":"HS256","crit":"exp"}"#,
            r#"{"alg":"HS256","crit":[1]}"#,
            r#"{"alg":"HS256","crit":null}"#,
        ] {
            assert!(!passes(header, &with_exp("")), "{header}");
        }
    }

    /// RFC 7515 and 7519, section 4 allow a parser to refuse a name that
    /// occurs two times. Bolt refuses it, so no reader can take the other one.
    #[test]
    fn a_duplicate_name_is_refused() {
        for claims in [
            with_exp(r#""sub":"7""#),
            with_exp(r#""role":"a","role":"b""#),
            with_exp(r#""is_staff":false,"is_staff":true"#),
            with_exp(r#""permissions":[],"permissions":["admin"]"#),
            format!(r#"{{"sub":"42","exp":1,"exp":{}}}"#, future_exp()),
        ] {
            assert!(!passes(r#"{"alg":"HS256"}"#, &claims), "{claims}");
        }
        for header in [
            r#"{"alg":"HS256","alg":"HS256"}"#,
            r#"{"alg":"HS256","kid":"a","kid":"a"}"#,
            r#"{"alg":"HS256","b64":true,"b64":true}"#,
        ] {
            assert!(!passes(header, &with_exp("")), "{header}");
        }
        // Bolt does not read another header parameter.
        assert!(passes(r#"{"alg":"HS256","x":1,"x":2}"#, &with_exp("")));
    }

    #[test]
    fn a_header_or_claims_that_is_not_a_json_object_is_refused() {
        let exp = future_exp();
        assert!(!passes(r#"["HS256"]"#, &with_exp("")));
        assert!(!passes(r#"{"alg":"HS256"}"#, &format!(r#"["42",{exp}]"#)));
        assert!(!passes(r#"{"alg":"HS256"}"#, "42"));
    }

    /// RFC 7515, section 5.2 and RFC 7519, section 7.2: each part is UTF-8,
    /// also in a value that Bolt does not read.
    #[test]
    fn a_byte_that_is_not_utf8_is_refused_in_each_value() {
        let exp = future_exp();
        let header = br#"{"alg":"HS256"}"#.as_slice();
        let claims = format!(r#"{{"sub":"42","exp":{exp}}}"#).into_bytes();
        let valid = |header: &[u8], claims: &[u8]| {
            validate(&sign_bytes(header, claims, Algorithm::HS256), None, 0).is_some()
        };
        assert!(valid(header, &claims));
        for bad_header in [
            b"{\"alg\":\"HS256\",\"x\":\"\xff\"}".as_slice(),
            b"{\"alg\":\"HS256\",\"x\":{\"a\":\"\xff\"}}",
            b"{\"alg\":\"HS256\",\"x\":[\"\xff\"]}",
        ] {
            assert!(!valid(bad_header, &claims), "{}", bad_header.escape_ascii());
        }
        for value in [
            b"\"is_staff\":{\"a\":\"\xff\"}".as_slice(),
            b"\"permissions\":[{\"a\":\"\xff\"}]",
            b"\"aud\":{\"\xff\":1}",
            b"\"amr\":[[\"\xff\"]]",
            b"\"x\":\"\xff\"",
        ] {
            let mut bad_claims = format!(r#"{{"sub":"42","exp":{exp},"#).into_bytes();
            bad_claims.extend_from_slice(value);
            bad_claims.push(b'}');
            assert!(!valid(header, &bad_claims), "{}", value.escape_ascii());
            assert!(parse_token_claims(&bad_claims).is_err());
        }
    }

    #[test]
    fn an_alg_that_is_not_an_allowed_name_is_refused() {
        for header in [
            r#"{"alg":"none"}"#,
            r#"{"alg":"hs256"}"#,
            r#"{"alg":""}"#,
            r#"{"alg":["HS256"]}"#,
            r#"{"typ":"JWT"}"#,
        ] {
            assert!(!passes(header, &with_exp("")), "{header}");
        }
    }

    /// RFC 7797: Bolt does not support a payload that is not base64url.
    #[test]
    fn an_unencoded_payload_is_refused() {
        assert!(!passes(r#"{"alg":"HS256","b64":false}"#, &with_exp("")));
        assert!(passes(r#"{"alg":"HS256","b64":true}"#, &with_exp("")));
        assert!(passes(r#"{"alg":"HS256","b64":"false"}"#, &with_exp("")));
    }

    /// RFC 7515, section 7.1 has three base64url parts with no padding.
    #[test]
    fn a_token_with_another_form_of_parts_is_refused() {
        let encode = |part: &str| URL_SAFE_NO_PAD.encode(part);
        let sign = |message: &str| {
            let signature = crypto::sign(
                message.as_bytes(),
                &EncodingKey::from_secret(SECRET.as_bytes()),
                Algorithm::HS256,
            )
            .unwrap();
            format!("{message}.{signature}")
        };
        let header = encode(r#"{"alg":"HS256"}"#);
        let claims = encode(&with_exp(""));
        assert!(validate(&sign(&format!("{header}.{claims}")), None, 0).is_some());
        for message in [
            format!("{header}.{claims}.{claims}"),
            header.clone(),
            format!("{header}==.{claims}"),
            format!("{header}.{claims}="),
        ] {
            assert!(validate(&sign(&message), None, 0).is_none(), "{message}");
        }
        let padded = sign(&format!("{header}.{claims}")) + "=";
        assert!(validate(&padded, None, 0).is_none());
    }

    #[test]
    fn an_audience_array_with_another_type_is_refused_only_when_checked() {
        let token = |aud: &str| sign_token(r#"{"alg":"HS256"}"#, &with_exp(aud), Algorithm::HS256);
        for aud in [
            r#""aud":["api",1]"#,
            r#""aud":123"#,
            r#""aud":null"#,
            r#""aud":[]"#,
            r#""aud":"""#,
        ] {
            assert!(validate(&token(aud), Some("api"), 0).is_none(), "{aud}");
            assert!(validate(&token(aud), None, 0).is_some(), "{aud}");
        }
        assert!(validate(&token(r#""aud":["web","api"]"#), Some("api"), 0).is_some());
    }

    #[test]
    fn an_issuer_of_another_type_is_refused_only_when_checked() {
        for iss in [r#""iss":123"#, r#""iss":null"#, r#""iss":["https://a"]"#] {
            let token = sign_token(r#"{"alg":"HS256"}"#, &with_exp(iss), Algorithm::HS256);
            let checked = decode_and_validate(
                &token,
                &hs256_key(),
                &[Algorithm::HS256],
                None,
                Some("https://a"),
                0,
                None,
            );
            assert!(checked.is_none(), "{iss}");
            assert!(validate(&token, None, 0).is_some(), "{iss}");
        }
    }

    /// Bolt reads the type of a token from these claims. A value that is not a
    /// string could hide a refresh token, so the token is refused.
    #[test]
    fn a_token_type_claim_that_is_not_a_string_is_refused() {
        for claim in [
            r#""typ":5"#,
            r#""typ":["refresh"]"#,
            r#""token_type":["refresh"]"#,
            r#""token_type":null"#,
            r#""token_use":{"id":true}"#,
        ] {
            for expected in [None, Some("refresh")] {
                // A refresh route needs `typ: "refresh"`. With it, only the
                // check of the other type claims refuses the token.
                let claims = match expected {
                    Some(_) if !claim.starts_with(r#""typ""#) => {
                        format!(r#""typ":"refresh",{claim}"#)
                    }
                    _ => claim.to_owned(),
                };
                let token = sign_token(r#"{"alg":"HS256"}"#, &with_exp(&claims), Algorithm::HS256);
                let checked = decode_and_validate(
                    &token,
                    &hs256_key(),
                    &[Algorithm::HS256],
                    None,
                    None,
                    0,
                    expected,
                );
                assert!(checked.is_none(), "{claim} {expected:?}");
            }
        }
    }

    /// A claim that grants something grants it only in its exact JSON form.
    #[test]
    fn a_grant_claim_grants_only_in_its_exact_form() {
        let context = |claims: &str| {
            let token = sign_token(r#"{"alg":"HS256"}"#, &with_exp(claims), Algorithm::HS256);
            AuthContext::from_jwt_claims(validate(&token, None, 0).unwrap(), "jwt", false)
        };
        for flags in [
            r#""is_staff":1,"is_superuser":"true""#,
            r#""is_staff":"yes","is_superuser":[true]"#,
            r#""is_staff":null,"is_superuser":{}"#,
        ] {
            let ctx = context(flags);
            assert!(!ctx.is_staff && !ctx.is_superuser, "{flags}");
            let claims = ctx.claims.unwrap();
            assert_eq!(
                (claims.is_staff, claims.is_superuser),
                (None, None),
                "{flags}"
            );
            let malformed = claims.malformed;
            assert!(malformed.contains(TypedClaim::IsStaff), "{flags}");
            assert!(malformed.contains(TypedClaim::IsSuperuser), "{flags}");
        }
        let ctx = context(r#""is_staff":true,"is_superuser":true"#);
        assert!(ctx.is_staff && ctx.is_superuser);

        for permissions in [
            r#""permissions":"admin""#,
            r#""permissions":["admin",1]"#,
            r#""permissions":{"admin":1}"#,
        ] {
            assert!(context(permissions).permissions.is_empty(), "{permissions}");
        }
        let ctx = context(r#""permissions":["read","write"],"amr":["pwd",2],"is_admin":1"#);
        assert_eq!(ctx.permissions.len(), 2);
        let claims = ctx.claims.unwrap();
        assert_eq!((claims.amr, claims.is_admin), (None, None));
    }

    fn time_check(claims: &str, now: f64, leeway: f64) -> Result<(), &'static str> {
        let claims = parse_token_claims(claims.as_bytes()).unwrap();
        check_time_claims(&claims, now, leeway)
    }

    /// PyJWT compares `int(exp) <= now - leeway` and `int(nbf) > now + leeway`
    /// with the current time as a float. Bolt removes the fraction of `exp` as
    /// PyJWT does. It does not round `nbf` and `iat` (RFC 7519, section 4.1.5).
    #[test]
    fn each_time_claim_has_its_boundary_on_each_side() {
        let second = 1_795_714_848.0;
        let now = second + 0.25;
        let exp = |exp: &str| time_check(&format!(r#"{{"exp":{exp}}}"#), now, 0.0);
        // The second of exp has started: expired, also for an integer exp.
        assert_eq!(exp("1795714848"), Err("token expired"));
        assert_eq!(exp("1795714848.75"), Err("token expired"));
        assert_eq!(exp("1795714849"), Ok(()));
        assert_eq!(exp("1795714849.5"), Ok(()));
        assert_eq!(exp("1795714847.999"), Err("token expired"));
        // With a fraction of now at or after the fraction of exp, PyJWT too.
        let late = time_check(r#"{"exp":1795714849.5}"#, second + 0.75, 0.0);
        assert_eq!(late, Ok(()));
        let leeway = |claims: &str| time_check(claims, now, 60.0);
        assert_eq!(leeway(r#"{"exp":1795714789}"#), Ok(()));
        assert_eq!(leeway(r#"{"exp":1795714788.9}"#), Err("token expired"));
        // The limit is exclusive: now must be before trunc(exp) + leeway.
        assert_eq!(
            time_check(r#"{"exp":1795714849}"#, 1_795_714_849.0, 0.0),
            Err("token expired")
        );
        assert_eq!(
            time_check(r#"{"exp":1795714789.5}"#, second + 1.0, 60.0),
            Err("token expired")
        );
        assert_eq!(
            time_check(r#"{"exp":1795714849}"#, 1_795_714_848.999_999, 0.0),
            Ok(())
        );
        // The decimal is read as the nearest f64, as Python does: 1795714848.9999998,
        // not 1795714849.0. Thus int(exp) is 1795714848, and the token expired.
        assert_eq!(exp("1795714848.99999985"), Err("token expired"));

        for (name, error) in [
            ("nbf", "token not yet valid (nbf)"),
            ("iat", "token issued in the future (iat)"),
        ] {
            let at = |time: &str, leeway: f64| {
                time_check(&format!(r#"{{"exp":2e9,"{name}":{time}}}"#), now, leeway)
            };
            assert_eq!(at("1795714848.25", 0.0), Ok(()), "{name}");
            assert_eq!(at("1795714848", 0.0), Ok(()), "{name}");
            assert_eq!(at("1795714848.5", 0.0), Err(error), "{name}");
            assert_eq!(at("1795714849", 0.0), Err(error), "{name}");
            assert_eq!(at("1795714908.25", 60.0), Ok(()), "{name}");
            assert_eq!(at("1795714908.5", 60.0), Err(error), "{name}");
        }
    }

    /// A NumericDate must be a time that a Python `datetime` holds: from
    /// 0001-01-01 to 9999-12-31T23:59:59.999 UTC. Out of that range, the claim
    /// is malformed, and an `exp`, `nbf` or `iat` fails the token. PyJWT
    /// accepts `"exp": 1e300`, and a revocation store then raised
    /// `OverflowError`.
    #[test]
    fn a_numeric_date_that_a_datetime_cannot_hold_fails_the_token() {
        for name in ["exp", "nbf", "iat"] {
            for time in [
                "253402300800",
                "253402300800.0",
                "-62135596801",
                "-62135596800.5",
                "1e300",
                "-1e300",
                "9223372036854775807",
                "18446744073709551615",
                "184467440737095516150",
            ] {
                let claims = format!(r#"{{"{name}":{time}}}"#);
                assert!(parse_token_claims(claims.as_bytes()).is_err(), "{claims}");
                let lenient: Claims = serde_json::from_str(&claims).unwrap();
                assert!(
                    lenient
                        .malformed
                        .contains(TypedClaim::from_name(name).unwrap())
                );
            }
            for (time, value) in [
                ("253402300799", Number::from(253_402_300_799i64)),
                ("253402300799.0", Number::from(253_402_300_799i64)),
                ("-62135596800", Number::from(-62_135_596_800i64)),
                (
                    "2.534023007995e11",
                    Number::from_f64(253_402_300_799.5).unwrap(),
                ),
            ] {
                let claims = parse_token_claims(format!(r#"{{"{name}":{time}}}"#).as_bytes());
                let field = |claims: Claims| match name {
                    "exp" => claims.exp,
                    "nbf" => claims.nbf,
                    _ => claims.iat,
                };
                assert_eq!(field(claims.unwrap()), Some(value), "{name}={time}");
            }
        }
        // The time checks with the limits, and with a huge leeway, do not wrap.
        let now = 1_795_714_848.5;
        let huge = i64::MAX as f64;
        for (claims, leeway, expected) in [
            (r#"{"exp":253402300799}"#, 0.0, Ok(())),
            (r#"{"exp":-62135596800}"#, 0.0, Err("token expired")),
            (r#"{"exp":-62135596800}"#, huge, Ok(())),
            (r#"{"exp":1,"nbf":253402300799}"#, huge, Ok(())),
            (
                r#"{"exp":253402300799,"nbf":253402300799}"#,
                0.0,
                Err("token not yet valid (nbf)"),
            ),
            (
                r#"{"exp":253402300799,"iat":253402300799}"#,
                0.0,
                Err("token issued in the future (iat)"),
            ),
            (
                r#"{"exp":253402300799,"nbf":-62135596800,"iat":-62135596800}"#,
                0.0,
                Ok(()),
            ),
        ] {
            assert_eq!(time_check(claims, now, leeway), expected, "{claims}");
        }
        // JSON has no Infinity or NaN, and a number beyond f64 is an error.
        for claims in [r#"{"exp":1e400}"#, r#"{"exp":Infinity}"#, r#"{"exp":NaN}"#] {
            assert!(parse_token_claims(claims.as_bytes()).is_err(), "{claims}");
        }
    }

    /// A whole number reaches Python as an int, also as `N.0` or `Ne0`. Only
    /// a number with a fraction is a float.
    #[test]
    fn a_numeric_date_reaches_python_as_an_int_unless_it_has_a_fraction() {
        Python::initialize();
        let claims = parse_token_claims(
            br#"{"exp":253402300799.0,"iat":1795714848.5,"nbf":-6.21355968e10}"#,
        )
        .unwrap();
        Python::attach(|py| {
            let dict = claims_to_dict(py, &claims);
            let item = |key: &str| dict.get_item(key).unwrap().unwrap();
            assert_eq!(item("exp").get_type().name().unwrap(), "int");
            assert_eq!(item("exp").extract::<i64>().unwrap(), 253_402_300_799);
            assert_eq!(item("iat").get_type().name().unwrap(), "float");
            assert_eq!(item("iat").extract::<f64>().unwrap(), 1_795_714_848.5);
            assert_eq!(item("nbf").get_type().name().unwrap(), "int");
            assert_eq!(item("nbf").extract::<i64>().unwrap(), -62_135_596_800);
        });
    }

    /// A whole number is an integer for a guard too: `N.0` matches `N`.
    #[test]
    fn a_whole_number_parses_as_an_integer() {
        for text in [
            "1795714848",
            "1795714848.0",
            "1795714848e0",
            "17957148.48e2",
        ] {
            let claims = parse_token_claims(
                format!(r#"{{"exp":{text},"iat":{text},"nbf":{text},"oat":{text},"ver":{text}}}"#)
                    .as_bytes(),
            )
            .unwrap();
            for time in [&claims.exp, &claims.iat, &claims.nbf] {
                assert_eq!(
                    time.as_ref().unwrap(),
                    &Number::from(1_795_714_848),
                    "{text}"
                );
            }
            assert_eq!(
                (claims.oat, claims.ver),
                (Some(1_795_714_848), Some(1_795_714_848))
            );
            assert!(claims.malformed.is_empty(), "{text}");
        }
        let claims = parse_token_claims(br#"{"exp":1.5,"oat":1.5,"ver":2e19}"#).unwrap();
        assert!(claims.exp.unwrap().is_f64());
        assert!(claims.malformed.contains(TypedClaim::Oat));
        assert!(claims.malformed.contains(TypedClaim::Ver));
    }

    /// A malformed claim reaches Python as `None`, a missing one not at all.
    #[test]
    fn a_malformed_claim_reaches_python_as_none() {
        Python::initialize();
        let token = sign_token(
            r#"{"alg":"HS256"}"#,
            &with_exp(
                r#""iss":5,"aud":[1],"fam":null,"oat":"1","ver":{},"amr":"pwd","is_staff":1,"is_superuser":"x","is_admin":[],"permissions":"admin","role":null"#,
            ),
            Algorithm::HS256,
        );
        let ctx = AuthContext::from_jwt_claims(validate(&token, None, 0).unwrap(), "jwt", false);
        Python::attach(|py| {
            let dict = claims_to_dict(py, ctx.claims.as_ref().unwrap());
            for name in [
                "iss",
                "aud",
                "fam",
                "oat",
                "ver",
                "amr",
                "is_staff",
                "is_superuser",
                "is_admin",
                "permissions",
                "role",
            ] {
                assert!(dict.get_item(name).unwrap().unwrap().is_none(), "{name}");
            }
            assert_eq!(dict.len(), 13);
            assert!(dict.get_item("jti").unwrap().is_none());
        });
    }

    /// The claims of an MCP principal are verified already. There a claim of
    /// another type is malformed, also one that Bolt checks on each token.
    #[test]
    fn lenient_claims_mark_a_claim_of_another_type_as_malformed() {
        let claims: Claims = serde_json::from_str(
            r#"{"sub":null,"jti":5,"typ":null,"exp":"soon","is_staff":1,"role":"x"}"#,
        )
        .unwrap();
        assert!(claims.sub.is_none() && claims.jti.is_none() && claims.typ.is_none());
        assert!(claims.exp.is_none() && claims.is_staff.is_none());
        let malformed: Vec<_> = claims.malformed.iter().collect();
        assert_eq!(
            malformed,
            [
                TypedClaim::Sub,
                TypedClaim::Jti,
                TypedClaim::Typ,
                TypedClaim::Exp,
                TypedClaim::IsStaff
            ]
        );
        assert_eq!(claims.extra["role"], "x");
        assert!(serde_json::from_str::<Claims>(r#"{"sub":"1","sub":"2"}"#).is_err());
    }

    /// Each typed claim has its own name and bit.
    #[test]
    fn each_typed_claim_has_its_name_and_bit() {
        let mut set = ClaimSet::default();
        for claim in TypedClaim::ALL {
            assert_eq!(TypedClaim::from_name(claim.name()), Some(claim));
            assert!(set.insert(claim), "{claim:?}");
            assert!(!set.insert(claim), "{claim:?}");
        }
        assert_eq!(set.iter().collect::<Vec<_>>(), TypedClaim::ALL);
        assert_eq!(TypedClaim::from_name("role"), None);
    }

    proptest! {
        /// The scheme has no case, and one or more spaces end it. Any other
        /// character there, a tab too, leaves the value as it is.
        #[test]
        fn strip_scheme_takes_one_or_more_spaces(
            upper in proptest::collection::vec(any::<bool>(), 6),
            spaces in 0usize..5,
            separator in prop::sample::select(vec![" ", "\t", "\u{a0}", "\r", "x", "é"]),
            rest in "[ A-Za-z0-9._~+/=\\t-]{0,20}",
        ) {
            let scheme: String = "bearer"
                .chars()
                .zip(&upper)
                .map(|(c, upper)| if *upper { c.to_ascii_uppercase() } else { c })
                .collect();
            let value = format!("{scheme}{separator}{}{rest}", " ".repeat(spaces));
            let expected = (separator == " ").then(|| rest.trim_start_matches(' '));
            prop_assert_eq!(strip_scheme(&value, "Bearer"), expected);
        }

        /// A JSON number of any form in the range of `datetime` is a
        /// NumericDate with the value that Python reads: the nearest f64. Out
        /// of that range, the token fails.
        #[test]
        fn a_json_number_parses_as_a_numeric_date(
            value in any::<f64>().prop_filter("finite", |v| v.is_finite()),
            date in -62_135_596_800.0..253_402_300_800.0f64,
            int in any::<i64>(),
            uint in any::<u64>(),
            whole in -62_135_596_800i64..253_402_300_800i64,
        ) {
            for text in [
                serde_json::to_string(&value).unwrap(),
                format!("{value:e}"),
                serde_json::to_string(&date).unwrap(),
                format!("{date:e}"),
                int.to_string(),
                uint.to_string(),
            ] {
                let claims = parse_token_claims(format!(r#"{{"exp":{text}}}"#).as_bytes());
                let expected: f64 = text.parse().unwrap();
                if NUMERIC_DATE_RANGE.contains(&expected) {
                    prop_assert_eq!(claims.unwrap().exp.unwrap().as_f64(), Some(expected), "{}", text);
                } else {
                    prop_assert!(claims.is_err(), "{}", text);
                }
            }
            let exact = parse_token_claims(format!(r#"{{"exp":{whole}}}"#).as_bytes()).unwrap();
            prop_assert_eq!(exact.exp.unwrap().as_i64(), Some(whole));
        }

        /// Any other JSON value of a checked time claim refuses the token.
        #[test]
        fn a_numeric_date_of_another_type_is_an_error(
            name in prop::sample::select(vec!["exp", "nbf", "iat"]),
            value in prop::sample::select(vec!["null", "true", "\"1795714848\"", "[1]", "{}", "\"\""]),
        ) {
            let claims = format!(r#"{{"exp":2e9,"{name}":{value}}}"#);
            let claims = if name == "exp" { format!(r#"{{"exp":{value}}}"#) } else { claims };
            prop_assert!(parse_token_claims(claims.as_bytes()).is_err());
        }
    }
}
