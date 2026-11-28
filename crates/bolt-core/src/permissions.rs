/// Permission guards for authorization checks
///
/// Guards are evaluated after authentication to determine if a request
/// should be allowed. There are exactly three guard types, mirroring the
/// Python API: `AllowAny`, `IsAuthenticated`, and `Requires` — the single
/// permission primitive. A `Requires` guard's claim name and expected
/// values are extracted from Python once at registration, so request-time
/// evaluation happens entirely in Rust with zero GIL overhead.
use crate::middleware::auth::{Audience, AuthContext, TypedClaim, json_number};
use bytes::Bytes;
use serde_json::{Number, Value as Json};
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
/// `TypedClaim::Permissions` is special: it reads the unified permission set
/// on `AuthContext`, which is populated from the JWT `permissions` claim OR
/// from `key_permissions` for API-key auth — so `Requires("permissions", ..)`
/// works for every backend. All other claims come from the token: the
/// typed ones live in `Claims` fields (the reader puts the rest into
/// `extra`), and backends that carry no claims never match.
#[derive(Debug, Clone, PartialEq)]
pub enum ClaimKey {
    Typed(TypedClaim),
    /// Any claim without a typed `Claims` field — looked up in `extra`.
    Extra(String),
}

impl ClaimKey {
    pub fn parse(name: String) -> Self {
        TypedClaim::from_name(&name).map_or(Self::Extra(name), Self::Typed)
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
    if matches!(resolved, ClaimValue::Malformed) {
        // A guard cannot read a claim in another JSON form. It refuses in
        // each form, `none_of` too: the token may hold a refused value there.
        return false;
    }
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
    /// A typed claim in another JSON form, also `null`.
    Malformed,
    Permissions(&'a HashSet<String>),
    Str(&'a str),
    Int(i64),
    /// A NumericDate claim (`exp`, `iat`, `nbf`), also with a fraction.
    Num(&'a Number),
    Bool(bool),
    StrList(&'a [String]),
    Json(&'a Json),
}

fn resolve_claim<'a>(ctx: &'a AuthContext, claim: &'a ClaimKey) -> ClaimValue<'a> {
    let typed = match claim {
        ClaimKey::Typed(typed) => *typed,
        ClaimKey::Extra(name) => {
            return match ctx
                .claims
                .as_ref()
                .and_then(|claims| claims.extra.get(name))
            {
                // A null claim is neither present nor matchable.
                None | Some(Json::Null) => ClaimValue::Absent,
                Some(value) => ClaimValue::Json(value),
            };
        }
    };
    if ctx
        .claims
        .as_ref()
        .is_some_and(|claims| claims.malformed.contains(typed))
    {
        return ClaimValue::Malformed;
    }
    if typed == TypedClaim::Permissions {
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
    fn opt_num(value: &Option<Number>) -> ClaimValue<'_> {
        value.as_ref().map_or(ClaimValue::Absent, ClaimValue::Num)
    }
    fn opt_bool(value: Option<bool>) -> ClaimValue<'static> {
        value.map_or(ClaimValue::Absent, ClaimValue::Bool)
    }

    match typed {
        TypedClaim::Permissions => unreachable!("handled above"),
        TypedClaim::Sub => opt_str(&claims.sub),
        TypedClaim::Iss => opt_str(&claims.iss),
        TypedClaim::Jti => opt_str(&claims.jti),
        TypedClaim::Typ => opt_str(&claims.typ),
        TypedClaim::Fam => opt_str(&claims.fam),
        TypedClaim::Exp => opt_num(&claims.exp),
        TypedClaim::Iat => opt_num(&claims.iat),
        TypedClaim::Nbf => opt_num(&claims.nbf),
        TypedClaim::Oat => opt_int(claims.oat),
        TypedClaim::Ver => opt_int(claims.ver),
        TypedClaim::IsStaff => opt_bool(claims.is_staff),
        TypedClaim::IsSuperuser => opt_bool(claims.is_superuser),
        TypedClaim::IsAdmin => opt_bool(claims.is_admin),
        TypedClaim::Amr => claims
            .amr
            .as_deref()
            .map_or(ClaimValue::Absent, ClaimValue::StrList),
        TypedClaim::Aud => match &claims.aud {
            None => ClaimValue::Absent,
            Some(Audience::Single(aud)) => ClaimValue::Str(aud),
            Some(Audience::Multiple(list)) => ClaimValue::StrList(list),
        },
    }
}

impl ClaimValue<'_> {
    fn is_present(&self) -> bool {
        match self {
            ClaimValue::Absent | ClaimValue::Malformed => false,
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
            ClaimValue::Absent | ClaimValue::Malformed => false,
            ClaimValue::Permissions(perms) => {
                expected.as_str().is_some_and(|perm| perms.contains(perm))
            }
            ClaimValue::Str(v) => expected.as_str() == Some(v),
            ClaimValue::Int(v) => expected.as_i64() == Some(*v),
            ClaimValue::Num(v) => expected.as_number() == Some(*v),
            ClaimValue::Bool(v) => expected.as_bool() == Some(*v),
            ClaimValue::StrList(list) => list.iter().any(|v| expected.as_str() == Some(v)),
            ClaimValue::Json(value) => match value {
                Json::Array(items) => items.iter().any(|item| same_json(item, expected)),
                other => same_json(other, expected),
            },
        }
    }
}

/// A private claim equals a guard value as in Python: the number `7.0` in
/// a token equals `7`. A guard value is in the form of [`json_number`].
fn same_json(claim: &Json, expected: &Json) -> bool {
    match (claim, expected) {
        (Json::Number(claim), Json::Number(expected)) if claim.is_f64() => {
            claim.as_f64().and_then(json_number).as_ref() == Some(expected)
        }
        _ => claim == expected,
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
            is_staff: Some(true),
            permissions: if permissions.is_empty() {
                None
            } else {
                Some(permissions.iter().map(|p| p.to_string()).collect())
            },
            extra: extra
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect::<HashMap<_, _>>(),
            ..Claims::default()
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

    /// `topics/permissions.md`: for `permissions`, present means one
    /// permission or more. An empty list gives no permission, for a JWT, an
    /// API key and a session alike.
    #[test]
    fn requires_permissions_needs_one_permission() {
        let present = requires("permissions", &[], Quantifier::Any);
        let jwt = |permissions: Vec<String>| {
            AuthContext::from_jwt_claims(
                Claims {
                    sub: Some("u1".to_string()),
                    permissions: Some(permissions),
                    ..Claims::default()
                },
                "jwt",
                false,
            )
        };
        assert!(matches!(
            evaluate_guards(&present, Some(&jwt(Vec::new()))),
            GuardResult::Forbidden(_)
        ));
        assert!(matches!(
            evaluate_guards(&present, Some(&jwt(vec!["blog.add".to_string()]))),
            GuardResult::Allow
        ));
        let no_key_permissions = AuthContext::from_api_key("k", &HashMap::new());
        assert!(matches!(
            evaluate_guards(&present, Some(&no_key_permissions)),
            GuardResult::Forbidden(_)
        ));
    }

    /// Each typed claim of `Claims` matches its guard, and another value does
    /// not. A typed claim that `ClaimKey::parse` sent to `Extra` never
    /// matches: serde puts a known claim in its field, not in `extra`.
    #[test]
    fn every_typed_claim_matches_its_guard() {
        let payload = serde_json::json!({
            "sub": "u1", "iss": "issuer", "jti": "id1", "typ": "access", "fam": "f1",
            "exp": 2_000_000_000i64, "iat": 1_000, "nbf": 1_001, "oat": 1_002, "ver": 3,
            "is_staff": true, "is_superuser": true, "is_admin": true,
            "amr": ["pwd", "otp"], "aud": "api", "permissions": ["read"],
        });
        let claims: Claims = serde_json::from_value(payload.clone()).unwrap();
        let ctx = AuthContext::from_jwt_claims(claims, "jwt", false);
        for (name, value) in payload.as_object().unwrap() {
            let (expected, other) = match value {
                Json::Array(items) => (items[0].clone(), Json::from("other")),
                Json::String(_) => (value.clone(), Json::from("other")),
                Json::Bool(flag) => (value.clone(), Json::from(!flag)),
                Json::Number(number) => (value.clone(), Json::from(number.as_i64().unwrap() + 1)),
                _ => unreachable!("the payload has no other type"),
            };
            assert_eq!(
                evaluate_guards(&any_of(name, &[expected]), Some(&ctx)),
                GuardResult::Allow,
                "{name}"
            );
            assert_eq!(
                evaluate_guards(&any_of(name, &[other]), Some(&ctx)),
                GuardResult::Forbidden(None),
                "{name}"
            );
        }
    }

    /// A NumericDate with a fraction matches the same JSON number only.
    #[test]
    fn a_fractional_time_claim_matches_its_guard() {
        let claims: Claims =
            serde_json::from_value(serde_json::json!({"exp": 2_000_000_000.5, "iat": 7})).unwrap();
        let ctx = AuthContext::from_jwt_claims(claims, "jwt", false);
        for (name, value, expected) in [
            ("exp", Json::from(2_000_000_000.5), GuardResult::Allow),
            (
                "exp",
                Json::from(2_000_000_000i64),
                GuardResult::Forbidden(None),
            ),
            ("iat", Json::from(7), GuardResult::Allow),
            ("iat", Json::from(7.5), GuardResult::Forbidden(None)),
        ] {
            assert_eq!(
                evaluate_guards(&any_of(name, &[value]), Some(&ctx)),
                expected,
                "{name}"
            );
        }
    }

    /// A private number equals a guard value of the same value, as in Python.
    /// A guard value is in the form that registration gives: `7.0` is `7`.
    #[test]
    fn a_private_number_matches_a_guard_value_of_the_same_value() {
        for (claim, value, expected) in [
            (Json::from(7.0), Json::from(7), GuardResult::Allow),
            (Json::from(7), Json::from(7), GuardResult::Allow),
            (
                serde_json::json!([1, 7.0]),
                Json::from(7),
                GuardResult::Allow,
            ),
            (Json::from(7.5), Json::from(7.5), GuardResult::Allow),
            (Json::from(7.5), Json::from(7), GuardResult::Forbidden(None)),
            (
                Json::from(7.0),
                Json::from("7"),
                GuardResult::Forbidden(None),
            ),
            (
                Json::from(1.0),
                Json::from(true),
                GuardResult::Forbidden(None),
            ),
        ] {
            let claims: Claims =
                serde_json::from_value(serde_json::json!({ "level": claim })).unwrap();
            let ctx = AuthContext::from_jwt_claims(claims, "jwt", false);
            assert_eq!(
                evaluate_guards(&any_of("level", std::slice::from_ref(&value)), Some(&ctx)),
                expected,
                "{claim} {value}"
            );
        }
    }

    /// A typed claim in another JSON form, also `null`, fails each guard
    /// form, `none_of` too. A missing claim passes `none_of`, and a claim of
    /// the exact form gives the usual answers.
    #[test]
    fn a_malformed_claim_fails_each_guard_form() {
        // Each claim that `Claims` types: a valid value and a scalar of another
        // JSON type. A token cannot have a malformed `sub`, `jti`, `typ`, `exp`,
        // `nbf` or `iat`, but the claims of an MCP principal can.
        let typed = [
            ("sub", Json::from("u1"), Json::from(1)),
            ("jti", Json::from("j1"), Json::from(1)),
            ("typ", Json::from("access"), Json::from(1)),
            (
                "exp",
                Json::from(2_000_000_000i64),
                Json::from("2000000000"),
            ),
            ("nbf", Json::from(1), Json::from(true)),
            ("iat", Json::from(1), Json::from("1")),
            ("iss", Json::from("https://a"), Json::from(1)),
            ("aud", Json::from("api"), Json::from(1)),
            ("fam", Json::from("f1"), Json::from(7)),
            ("oat", Json::from(1), Json::from(1.5)),
            ("ver", Json::from(1), Json::from("1")),
            ("amr", Json::from("pwd"), Json::from("pwd")),
            ("is_staff", Json::from(true), Json::from(1)),
            ("is_superuser", Json::from(true), Json::from("true")),
            ("is_admin", Json::from(true), Json::from("false")),
            ("permissions", Json::from("admin"), Json::from("admin")),
        ];
        // The guard forms: (name, values, quantifier). No value is a presence check.
        let forms = |value: &Json| {
            let other = match value {
                Json::Bool(flag) => Json::from(!flag),
                Json::Number(_) => Json::from(3),
                _ => Json::from("other"),
            };
            [
                ("presence", vec![], Quantifier::Any),
                ("equality", vec![value.clone()], Quantifier::Any),
                ("any_of", vec![value.clone(), other], Quantifier::Any),
                ("all_of", vec![value.clone()], Quantifier::All),
                ("none_of", vec![value.clone()], Quantifier::None),
            ]
        };
        for (name, valid, wrong_scalar) in typed {
            let list_claim = matches!(name, "amr" | "permissions");
            let malformed = [
                wrong_scalar,
                Json::Array(vec![valid.clone(), Json::from(1)]),
                Json::Null,
                serde_json::json!({ "x": valid.clone() }),
            ];
            for value in malformed {
                let claims: Claims =
                    serde_json::from_value(serde_json::json!({ name: value })).unwrap();
                let ctx = AuthContext::from_jwt_claims(claims, "jwt", false);
                for (form, values, quantifier) in forms(&valid) {
                    let guard = requires(name, &values, quantifier);
                    assert_eq!(
                        evaluate_guards(&guard, Some(&ctx)),
                        GuardResult::Forbidden(None),
                        "{name}={value} {form}"
                    );
                }
            }

            let exact = if list_claim {
                Json::Array(vec![valid.clone()])
            } else {
                valid.clone()
            };
            let claims: Claims =
                serde_json::from_value(serde_json::json!({ name: exact })).unwrap();
            let present = AuthContext::from_jwt_claims(claims, "jwt", false);
            let absent = AuthContext::from_jwt_claims(Claims::default(), "jwt", false);
            for (form, values, quantifier) in forms(&valid) {
                let guard = requires(name, &values, quantifier);
                let (allowed_when_present, allowed_when_absent) = match form {
                    "none_of" => (false, true),
                    _ => (true, false),
                };
                let verdict = |allowed: bool| {
                    if allowed {
                        GuardResult::Allow
                    } else {
                        GuardResult::Forbidden(None)
                    }
                };
                assert_eq!(
                    evaluate_guards(&guard, Some(&present)),
                    verdict(allowed_when_present),
                    "{name}={exact} {form}"
                );
                assert_eq!(
                    evaluate_guards(&guard, Some(&absent)),
                    verdict(allowed_when_absent),
                    "{name} absent {form}"
                );
            }
        }
    }

    #[test]
    fn is_authenticated_needs_a_user() {
        let guards = GuardSet::from_guards(vec![Guard::IsAuthenticated]);
        let mut ctx = jwt_ctx(&[], &[]);
        assert_eq!(evaluate_guards(&guards, Some(&ctx)), GuardResult::Allow);
        ctx.user_id = None;
        assert_eq!(
            evaluate_guards(&guards, Some(&ctx)),
            GuardResult::Unauthorized
        );
        assert_eq!(evaluate_guards(&guards, None), GuardResult::Unauthorized);
    }

    /// A bare `Requires(claim)` checks that the claim is present.
    #[test]
    fn a_presence_check_needs_the_claim() {
        let with_role = jwt_ctx(&["read"], &[("role", Json::from("client"))]);
        let without = jwt_ctx(&[], &[]);
        for claim in ["role", "permissions"] {
            assert_eq!(
                evaluate_guards(&any_of(claim, &[]), Some(&with_role)),
                GuardResult::Allow,
                "{claim}"
            );
            assert_eq!(
                evaluate_guards(&any_of(claim, &[]), Some(&without)),
                GuardResult::Forbidden(None),
                "{claim}"
            );
            assert_eq!(
                evaluate_guards(&none_of(claim, &[]), Some(&with_role)),
                GuardResult::Forbidden(None),
                "{claim}"
            );
            assert_eq!(
                evaluate_guards(&none_of(claim, &[]), Some(&without)),
                GuardResult::Allow,
                "{claim}"
            );
        }
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
