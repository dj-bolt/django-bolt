/// Permission guards for authorization checks
///
/// Guards are evaluated after authentication to determine if a request
/// should be allowed. There are exactly three guard types, mirroring the
/// Python API: `AllowAny`, `IsAuthenticated`, and `Requires` — the single
/// permission primitive. A `Requires` guard's claim name and expected
/// values are extracted from Python once at registration, so request-time
/// evaluation happens entirely in Rust with zero GIL overhead.
use crate::middleware::auth::{Audience, AuthContext};
use bytes::Bytes;
use serde_json::Value as Json;
use std::collections::HashSet;
use std::sync::Arc;

/// How a `Requires` guard quantifies its expected values over the claim.
/// One tri-state rather than a pair of booleans, so the meaningless
/// "all of these AND none of these" combination cannot be represented.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Quantifier {
    /// The claim must match at least one value (positional values in Python).
    Any,
    /// The claim must match every value (`all_of=`).
    All,
    /// The claim must match none of the values (`none_of=`).
    None,
}

/// A custom denial detail, built once at registration.
///
/// Both representations are pre-computed: `body` is the exact JSON response
/// bytes (escaped by serde, so a quote or backslash in the message cannot
/// emit malformed JSON), and `text` is the raw string for call paths that
/// surface the reason as a Python exception rather than an HTTP response.
#[derive(Debug, PartialEq)]
pub struct GuardDenial {
    pub text: String,
    pub body: Bytes,
}

impl GuardDenial {
    pub fn new(text: String) -> Self {
        let body = Bytes::from(
            serde_json::to_vec(&serde_json::json!({ "detail": &text }))
                .expect("a JSON string always serializes"),
        );
        Self { text, body }
    }
}

/// A claim name compiled into a direct selector at registration, so
/// request-time evaluation never string-matches the claim name.
///
/// `Permissions` is special: it reads the unified permission set on
/// `AuthContext`, which is populated from the JWT `permissions` claim OR
/// from `key_permissions` for API-key auth — so `Requires("permissions", ..)`
/// works for every backend. All other claims come from the token: the
/// registered ones live in typed `Claims` fields (serde flattens the rest
/// into `extra`), and backends that carry no claims never match.
#[derive(Debug, Clone, PartialEq)]
pub enum ClaimKey {
    Permissions,
    Sub,
    Iss,
    Jti,
    Typ,
    Fam,
    Exp,
    Iat,
    Nbf,
    Oat,
    Ver,
    IsStaff,
    IsSuperuser,
    IsAdmin,
    Amr,
    Aud,
    /// Any claim without a typed `Claims` field — looked up in `extra`.
    Extra(String),
}

impl ClaimKey {
    /// Must cover every typed field on `Claims`: a field missed here would
    /// fall through to `Extra`, which serde only populates with *unknown*
    /// claims, so the guard would silently never match.
    pub fn parse(name: String) -> Self {
        match name.as_str() {
            "permissions" => Self::Permissions,
            "sub" => Self::Sub,
            "iss" => Self::Iss,
            "jti" => Self::Jti,
            "typ" => Self::Typ,
            "fam" => Self::Fam,
            "exp" => Self::Exp,
            "iat" => Self::Iat,
            "nbf" => Self::Nbf,
            "oat" => Self::Oat,
            "ver" => Self::Ver,
            "is_staff" => Self::IsStaff,
            "is_superuser" => Self::IsSuperuser,
            "is_admin" => Self::IsAdmin,
            "amr" => Self::Amr,
            "aud" => Self::Aud,
            _ => Self::Extra(name),
        }
    }
}

/// Guard/permission types
#[derive(Debug, Clone)]
pub enum Guard {
    AllowAny,
    IsAuthenticated,
    /// The permission check, compiled from Python `Requires(...)` at
    /// registration.
    ///
    /// - `values` empty → the claim must be present (and non-null).
    /// - `quantifier` → how the values are matched (any / all / none).
    /// - `denial` → the custom 403 detail, if the route declared one.
    Requires {
        claim: ClaimKey,
        values: Vec<Json>,
        quantifier: Quantifier,
        denial: Option<Arc<GuardDenial>>,
    },
}

/// A route's guards, with everything the request path needs to know about
/// them resolved at registration. Constructing through `from_guards` is the
/// only way to set the flags, so they cannot drift from the list.
#[derive(Debug, Clone, Default)]
pub struct GuardSet {
    guards: Vec<Guard>,
    /// Any guard can actually reject a request (i.e. is not `AllowAny`).
    /// Drives whether the route extracts headers and runs the auth pipeline.
    enforcing: bool,
}

impl GuardSet {
    pub fn from_guards(guards: Vec<Guard>) -> Self {
        Self {
            enforcing: guards.iter().any(|guard| !matches!(guard, Guard::AllowAny)),
            guards,
        }
    }

    #[inline(always)]
    pub fn is_empty(&self) -> bool {
        self.guards.is_empty()
    }

    /// Whether any guard can reject a request. `[]` and `[AllowAny]` cannot.
    #[inline(always)]
    pub fn is_enforcing(&self) -> bool {
        self.enforcing
    }
}

/// Evaluate guards against an optional AuthContext
/// Returns true if all guards pass, false otherwise
///
/// Guards are evaluated in declaration order and short-circuit on the first
/// verdict.
pub fn evaluate_guards(guard_set: &GuardSet, auth_ctx: Option<&AuthContext>) -> GuardResult {
    let guards = &guard_set.guards;

    // If no guards configured, allow by default
    if guards.is_empty() {
        return GuardResult::Allow;
    }

    // Check each guard
    for guard in guards {
        match guard {
            Guard::AllowAny => {
                // AllowAny short-circuits - allow immediately
                return GuardResult::Allow;
            }
            Guard::IsAuthenticated => {
                if auth_ctx.is_none() || auth_ctx.unwrap().user_id.is_none() {
                    return GuardResult::Unauthorized;
                }
            }
            Guard::Requires {
                claim,
                values,
                quantifier,
                denial,
            } => match auth_ctx {
                // Every `Requires` — including a `none_of` one — fails closed
                // without a credential. A request carrying no claims matches
                // nothing, so treating "no match" as success here would let
                // anonymous callers satisfy an exclusion guard.
                None => return GuardResult::Unauthorized,
                Some(ctx) => {
                    if !requires_matches(ctx, claim, values, *quantifier) {
                        return GuardResult::Forbidden(denial.clone());
                    }
                }
            },
        }
    }

    GuardResult::Allow
}

/// Evaluate one `Requires` guard against the authenticated context. The
/// claim's value is resolved exactly once, then compared against each
/// expected value.
fn requires_matches(
    ctx: &AuthContext,
    claim: &ClaimKey,
    values: &[Json],
    quantifier: Quantifier,
) -> bool {
    let resolved = resolve_claim(ctx, claim);
    if values.is_empty() {
        // Python rejects an empty `none_of`, so this arm is only reachable
        // through a bare presence check; the inverse is still the coherent
        // reading of "none of nothing".
        return match quantifier {
            Quantifier::None => !resolved.is_present(),
            _ => resolved.is_present(),
        };
    }
    match quantifier {
        Quantifier::Any => values.iter().any(|v| resolved.matches(v)),
        Quantifier::All => values.iter().all(|v| resolved.matches(v)),
        Quantifier::None => !values.iter().any(|v| resolved.matches(v)),
    }
}

/// A claim's value, resolved once per request from the `AuthContext`.
enum ClaimValue<'a> {
    Absent,
    Permissions(&'a HashSet<String>),
    Str(&'a str),
    Int(i64),
    Bool(bool),
    StrList(&'a [String]),
    Json(&'a Json),
}

fn resolve_claim<'a>(ctx: &'a AuthContext, claim: &'a ClaimKey) -> ClaimValue<'a> {
    if matches!(claim, ClaimKey::Permissions) {
        return ClaimValue::Permissions(&ctx.permissions);
    }
    let claims = match &ctx.claims {
        Some(claims) => claims,
        None => return ClaimValue::Absent,
    };

    fn opt_str(value: &Option<String>) -> ClaimValue<'_> {
        value.as_deref().map_or(ClaimValue::Absent, ClaimValue::Str)
    }
    fn opt_int(value: Option<i64>) -> ClaimValue<'static> {
        value.map_or(ClaimValue::Absent, ClaimValue::Int)
    }
    fn opt_bool(value: Option<bool>) -> ClaimValue<'static> {
        value.map_or(ClaimValue::Absent, ClaimValue::Bool)
    }

    match claim {
        ClaimKey::Permissions => unreachable!("handled above"),
        ClaimKey::Sub => opt_str(&claims.sub),
        ClaimKey::Iss => opt_str(&claims.iss),
        ClaimKey::Jti => opt_str(&claims.jti),
        ClaimKey::Typ => opt_str(&claims.typ),
        ClaimKey::Fam => opt_str(&claims.fam),
        ClaimKey::Exp => opt_int(claims.exp),
        ClaimKey::Iat => opt_int(claims.iat),
        ClaimKey::Nbf => opt_int(claims.nbf),
        ClaimKey::Oat => opt_int(claims.oat),
        ClaimKey::Ver => opt_int(claims.ver),
        ClaimKey::IsStaff => opt_bool(claims.is_staff),
        ClaimKey::IsSuperuser => opt_bool(claims.is_superuser),
        ClaimKey::IsAdmin => opt_bool(claims.is_admin),
        ClaimKey::Amr => claims
            .amr
            .as_deref()
            .map_or(ClaimValue::Absent, ClaimValue::StrList),
        ClaimKey::Aud => match &claims.aud {
            None => ClaimValue::Absent,
            Some(Audience::Single(aud)) => ClaimValue::Str(aud),
            Some(Audience::Multiple(list)) => ClaimValue::StrList(list),
        },
        ClaimKey::Extra(name) => match claims.extra.get(name) {
            // A null claim is neither present nor matchable.
            None | Some(Json::Null) => ClaimValue::Absent,
            Some(value) => ClaimValue::Json(value),
        },
    }
}

impl ClaimValue<'_> {
    fn is_present(&self) -> bool {
        match self {
            ClaimValue::Absent => false,
            ClaimValue::Permissions(perms) => !perms.is_empty(),
            // A bare `Requires("flag")` on a boolean claim means "the flag is
            // set" — `flag: false` must not satisfy a presence check.
            ClaimValue::Bool(v) => *v,
            ClaimValue::Json(Json::Bool(v)) => *v,
            _ => true,
        }
    }

    /// A scalar claim matches by equality; a list claim by membership.
    fn matches(&self, expected: &Json) -> bool {
        match self {
            ClaimValue::Absent => false,
            ClaimValue::Permissions(perms) => {
                expected.as_str().is_some_and(|perm| perms.contains(perm))
            }
            ClaimValue::Str(v) => expected.as_str() == Some(v),
            ClaimValue::Int(v) => expected.as_i64() == Some(*v),
            ClaimValue::Bool(v) => expected.as_bool() == Some(*v),
            ClaimValue::StrList(list) => list.iter().any(|v| expected.as_str() == Some(v)),
            ClaimValue::Json(value) => match value {
                Json::Array(items) => items.contains(expected),
                other => *other == expected,
            },
        }
    }
}

/// Result of guard evaluation
///
/// `Forbidden` carries the failing guard's custom denial, if it declared one.
/// Guards short-circuit on the first verdict, so this is the first failing
/// guard in declaration order. `Unauthorized` deliberately carries nothing:
/// the 401 body is uniform and discloses no reason.
#[derive(Debug, Clone, PartialEq)]
pub enum GuardResult {
    Allow,
    Unauthorized,                        // 401 - not authenticated
    Forbidden(Option<Arc<GuardDenial>>), // 403 - authenticated but lacking permission
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::auth::Claims;
    use std::collections::HashMap;

    fn jwt_ctx(permissions: &[&str], extra: &[(&str, Json)]) -> AuthContext {
        let claims = Claims {
            sub: Some("user1".to_string()),
            exp: None,
            iat: None,
            nbf: None,
            aud: None,
            iss: None,
            jti: None,
            typ: None,
            fam: None,
            oat: None,
            ver: None,
            amr: None,
            is_staff: Some(true),
            is_superuser: None,
            is_admin: None,
            permissions: if permissions.is_empty() {
                None
            } else {
                Some(permissions.iter().map(|p| p.to_string()).collect())
            },
            extra: extra
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect::<HashMap<_, _>>(),
        };
        AuthContext::from_jwt_claims(claims, "jwt", false)
    }

    fn requires(claim: &str, values: &[Json], quantifier: Quantifier) -> GuardSet {
        requires_with(claim, values, quantifier, None)
    }

    fn requires_with(
        claim: &str,
        values: &[Json],
        quantifier: Quantifier,
        denial: Option<Arc<GuardDenial>>,
    ) -> GuardSet {
        GuardSet::from_guards(vec![Guard::Requires {
            claim: ClaimKey::parse(claim.to_string()),
            values: values.to_vec(),
            quantifier,
            denial,
        }])
    }

    fn any_of(claim: &str, values: &[Json]) -> GuardSet {
        requires(claim, values, Quantifier::Any)
    }

    fn none_of(claim: &str, values: &[Json]) -> GuardSet {
        requires(claim, values, Quantifier::None)
    }

    #[test]
    fn extra_claim_string_equality() {
        let ctx = jwt_ctx(&[], &[("role", Json::from("client"))]);
        assert_eq!(
            evaluate_guards(&any_of("role", &[Json::from("client")]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("role", &[Json::from("vendor")]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn any_of_matches_either_value() {
        let ctx = jwt_ctx(&[], &[("role", Json::from("vip"))]);
        assert_eq!(
            evaluate_guards(
                &any_of("role", &[Json::from("client"), Json::from("vip")]),
                Some(&ctx)
            ),
            GuardResult::Allow
        );
    }

    #[test]
    fn missing_claim_is_forbidden_when_authenticated() {
        let ctx = jwt_ctx(&[], &[]);
        assert_eq!(
            evaluate_guards(&any_of("role", &[Json::from("client")]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn unauthenticated_is_401() {
        assert_eq!(
            evaluate_guards(&any_of("role", &[Json::from("client")]), None),
            GuardResult::Unauthorized
        );
    }

    #[test]
    fn list_claim_matches_on_membership() {
        let ctx = jwt_ctx(
            &[],
            &[(
                "roles",
                Json::Array(vec![Json::from("beta"), Json::from("client")]),
            )],
        );
        assert_eq!(
            evaluate_guards(&any_of("roles", &[Json::from("client")]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("roles", &[Json::from("admin")]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn match_all_requires_every_value() {
        let ctx = jwt_ctx(
            &[],
            &[("roles", Json::Array(vec![Json::from("a"), Json::from("b")]))],
        );
        assert_eq!(
            evaluate_guards(
                &requires(
                    "roles",
                    &[Json::from("a"), Json::from("b")],
                    Quantifier::All
                ),
                Some(&ctx)
            ),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(
                &requires(
                    "roles",
                    &[Json::from("a"), Json::from("c")],
                    Quantifier::All
                ),
                Some(&ctx)
            ),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn empty_values_is_presence_check() {
        let ctx = jwt_ctx(&[], &[("tenant", Json::from("acme"))]);
        assert_eq!(
            evaluate_guards(&any_of("tenant", &[]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("missing", &[]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn null_claim_is_not_present() {
        let ctx = jwt_ctx(&[], &[("tenant", Json::Null)]);
        assert_eq!(
            evaluate_guards(&any_of("tenant", &[]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn presence_check_on_false_boolean_claim_is_forbidden() {
        // `Requires("beta")` on a boolean claim means "the flag is set" — a
        // token carrying `beta: false` must not pass a bare presence check.
        let ctx = jwt_ctx(&[], &[("beta", Json::from(false))]);
        assert_eq!(
            evaluate_guards(&any_of("beta", &[]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
        let ctx = jwt_ctx(&[], &[("beta", Json::from(true))]);
        assert_eq!(
            evaluate_guards(&any_of("beta", &[]), Some(&ctx)),
            GuardResult::Allow
        );
    }

    #[test]
    fn presence_check_on_typed_boolean_claim_requires_true() {
        // Same rule for the typed standard claims (is_staff/is_superuser/is_admin):
        // jwt_ctx sets is_staff=true and leaves is_superuser unset.
        let ctx = jwt_ctx(&[], &[]);
        assert_eq!(
            evaluate_guards(&any_of("is_staff", &[]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("is_superuser", &[]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
        // An explicitly-false typed claim is not "present" either.
        let mut ctx = jwt_ctx(&[], &[]);
        if let Some(claims) = ctx.claims.as_mut() {
            claims.is_superuser = Some(false);
        }
        assert_eq!(
            evaluate_guards(&any_of("is_superuser", &[]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
        // But a false boolean still matches an explicit `Requires("x", False)`.
        assert_eq!(
            evaluate_guards(&any_of("is_superuser", &[Json::from(false)]), Some(&ctx)),
            GuardResult::Allow
        );
    }

    #[test]
    fn int_and_bool_claims_match() {
        let ctx = jwt_ctx(&[], &[("level", Json::from(5)), ("beta", Json::from(true))]);
        assert_eq!(
            evaluate_guards(&any_of("level", &[Json::from(5)]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("level", &[Json::from(4)]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
        assert_eq!(
            evaluate_guards(&any_of("beta", &[Json::from(true)]), Some(&ctx)),
            GuardResult::Allow
        );
    }

    #[test]
    fn standard_claims_are_reachable() {
        let ctx = jwt_ctx(&[], &[]);
        // sub/is_staff are typed fields, not extra claims — still guardable.
        assert_eq!(
            evaluate_guards(&any_of("sub", &[Json::from("user1")]), Some(&ctx)),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(&any_of("is_staff", &[Json::from(true)]), Some(&ctx)),
            GuardResult::Allow
        );
    }

    #[test]
    fn permissions_claim_reads_jwt_permission_set() {
        let ctx = jwt_ctx(&["blog.add", "blog.change"], &[]);
        assert_eq!(
            evaluate_guards(
                &any_of("permissions", &[Json::from("blog.add")]),
                Some(&ctx)
            ),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(
                &requires(
                    "permissions",
                    &[Json::from("blog.add"), Json::from("blog.change")],
                    Quantifier::All
                ),
                Some(&ctx)
            ),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(
                &requires(
                    "permissions",
                    &[Json::from("blog.add"), Json::from("blog.delete")],
                    Quantifier::All
                ),
                Some(&ctx)
            ),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn permissions_claim_reads_api_key_permissions() {
        // API keys have no claims, but Requires("permissions", ...) must
        // still work through key_permissions → AuthContext.permissions.
        let mut key_perms = HashMap::new();
        key_perms.insert("k1".to_string(), vec!["can.read".to_string()]);
        let ctx = AuthContext::from_api_key("k1", &key_perms);
        assert_eq!(
            evaluate_guards(
                &any_of("permissions", &[Json::from("can.read")]),
                Some(&ctx)
            ),
            GuardResult::Allow
        );
        assert_eq!(
            evaluate_guards(
                &any_of("permissions", &[Json::from("can.write")]),
                Some(&ctx)
            ),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn non_permission_claim_never_matches_for_api_keys() {
        let ctx = AuthContext::from_api_key("k1", &HashMap::new());
        assert_eq!(
            evaluate_guards(&any_of("role", &[Json::from("client")]), Some(&ctx)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn none_of_excludes_listed_values() {
        let banned = jwt_ctx(&[], &[("role", Json::from("banned"))]);
        let ok = jwt_ctx(&[], &[("role", Json::from("client"))]);
        let guards = none_of("role", &[Json::from("banned"), Json::from("suspended")]);
        assert_eq!(
            evaluate_guards(&guards, Some(&banned)),
            GuardResult::Forbidden(None)
        );
        assert_eq!(evaluate_guards(&guards, Some(&ok)), GuardResult::Allow);
    }

    #[test]
    fn none_of_passes_when_claim_is_absent() {
        let ctx = jwt_ctx(&[], &[]);
        assert_eq!(
            evaluate_guards(&none_of("role", &[Json::from("banned")]), Some(&ctx)),
            GuardResult::Allow
        );
    }

    #[test]
    fn none_of_is_401_without_credentials() {
        // The security property: a request with no claims matches nothing, so
        // a naive "did not match" would let anonymous callers through.
        assert_eq!(
            evaluate_guards(&none_of("role", &[Json::from("banned")]), None),
            GuardResult::Unauthorized
        );
    }

    #[test]
    fn none_of_checks_list_claim_membership() {
        let guards = none_of("roles", &[Json::from("banned")]);
        let clean = jwt_ctx(&[], &[("roles", serde_json::json!(["a", "b"]))]);
        let dirty = jwt_ctx(&[], &[("roles", serde_json::json!(["a", "banned"]))]);
        assert_eq!(evaluate_guards(&guards, Some(&clean)), GuardResult::Allow);
        assert_eq!(
            evaluate_guards(&guards, Some(&dirty)),
            GuardResult::Forbidden(None)
        );
    }

    #[test]
    fn denial_is_attached_to_the_forbidden_verdict() {
        let ctx = jwt_ctx(&[], &[("role", Json::from("vendor"))]);
        let denial = Arc::new(GuardDenial::new("Client accounts only".to_string()));
        let guards = requires_with(
            "role",
            &[Json::from("client")],
            Quantifier::Any,
            Some(denial),
        );
        match evaluate_guards(&guards, Some(&ctx)) {
            GuardResult::Forbidden(Some(denial)) => {
                assert_eq!(denial.text, "Client accounts only");
                assert_eq!(&denial.body[..], br#"{"detail":"Client accounts only"}"#);
            }
            other => panic!("expected a denial, got {other:?}"),
        }
    }

    #[test]
    fn denial_body_escapes_json() {
        let denial = GuardDenial::new(r#"Say "no" — \ ok"#.to_string());
        let parsed: Json = serde_json::from_slice(&denial.body).unwrap();
        assert_eq!(parsed["detail"], Json::from(r#"Say "no" — \ ok"#));
    }

    #[test]
    fn denial_is_not_attached_to_unauthorized() {
        // A custom message must not leak to a caller that never authenticated.
        let denial = Arc::new(GuardDenial::new("Client accounts only".to_string()));
        let guards = requires_with(
            "role",
            &[Json::from("client")],
            Quantifier::Any,
            Some(denial),
        );
        assert_eq!(evaluate_guards(&guards, None), GuardResult::Unauthorized);
    }

    #[test]
    fn allow_any_only_is_not_enforcing() {
        assert!(!GuardSet::from_guards(vec![Guard::AllowAny]).is_enforcing());
        assert!(
            GuardSet::from_guards(vec![Guard::AllowAny, Guard::IsAuthenticated]).is_enforcing()
        );
    }
}
