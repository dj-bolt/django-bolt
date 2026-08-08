//! MRTR `requestState` policy over rmcp's HMAC codec.
//!
//! The sealed state carries everything a stateless retry needs: which tool it
//! belongs to, digests binding it to the exact arguments and principal
//! (anti-replay per spec: reject state presented by a different principal or
//! on a request that does not match), and the accumulated client responses
//! from every prior round trip.

use std::collections::BTreeMap;

use rmcp::model::{RequestStateCodec, RequestStateError, SealOptions};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Sealed-state TTL. Long enough for a human to answer an elicitation prompt,
/// short enough to bound replay windows (spec: "a short expiry").
const STATE_TTL_SECS: u64 = 600;

#[derive(Serialize, Deserialize)]
pub struct McpRequestState {
    /// Tool name the state belongs to.
    pub tool: String,
    /// SHA-256 hex of the canonicalized arguments object.
    pub args_hash: String,
    /// SHA-256 hex of the authenticated principal identity ("anon" when none).
    pub principal_hash: String,
    /// Accumulated client responses from prior rounds, keyed by input key.
    pub responses: BTreeMap<String, Value>,
}

/// Digest of the tool-call arguments. `serde_json::Value` maps have sorted
/// (BTreeMap) keys, so serialization is canonical for equal objects.
pub fn args_hash(arguments: Option<&rmcp::model::JsonObject>) -> String {
    let mut hasher = Sha256::new();
    match arguments {
        Some(args) => {
            // JsonObject is serde_json::Map; with the default (order-preserving)
            // feature its iteration order follows insertion. Re-collect into a
            // BTreeMap for a canonical digest independent of client key order.
            let canonical: BTreeMap<&String, &Value> = args.iter().collect();
            hasher.update(serde_json::to_vec(&canonical).unwrap_or_default());
        }
        None => hasher.update(b"null"),
    }
    hex(&hasher.finalize())
}

/// Digest of the authenticated principal. Uses the auth context's user id and
/// backend so state cannot be replayed across users or auth methods.
pub fn principal_hash(auth: Option<&crate::middleware::auth::AuthContext>) -> String {
    let mut hasher = Sha256::new();
    match auth {
        Some(ctx) => {
            hasher.update(ctx.user_id.as_deref().unwrap_or("").as_bytes());
            hasher.update(b"\x00");
            hasher.update(ctx.backend.as_bytes());
        }
        None => hasher.update(b"anon"),
    }
    hex(&hasher.finalize())
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

pub fn seal(
    codec: &RequestStateCodec,
    state: &McpRequestState,
) -> Result<String, RequestStateError> {
    codec.seal_json_with(
        state,
        &SealOptions::new().ttl(std::time::Duration::from_secs(STATE_TTL_SECS)),
    )
}

/// Open and verify sealed state for a retry of `tool` with `arguments` by the
/// current principal. Any failure (bad HMAC, expired, wrong tool/args/user)
/// returns `Err(reason)` — callers turn it into an in-band tool error.
pub fn open_verified(
    codec: &RequestStateCodec,
    sealed: &str,
    tool: &str,
    arguments: Option<&rmcp::model::JsonObject>,
    auth: Option<&crate::middleware::auth::AuthContext>,
) -> Result<McpRequestState, String> {
    let state: McpRequestState = codec
        .open_json(sealed)
        .map_err(|e| format!("invalid requestState: {e}"))?;
    if state.tool != tool {
        return Err("requestState does not match this tool".to_string());
    }
    if state.args_hash != args_hash(arguments) {
        return Err("requestState does not match the request arguments".to_string());
    }
    if state.principal_hash != principal_hash(auth) {
        return Err("requestState does not match the authenticated principal".to_string());
    }
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn codec() -> RequestStateCodec {
        RequestStateCodec::new(vec![7u8; 32])
    }

    fn state() -> McpRequestState {
        McpRequestState {
            tool: "t".into(),
            args_hash: args_hash(None),
            principal_hash: principal_hash(None),
            responses: BTreeMap::new(),
        }
    }

    #[test]
    fn seal_open_roundtrip() {
        let c = codec();
        let sealed = seal(&c, &state()).unwrap();
        let opened = open_verified(&c, &sealed, "t", None, None).unwrap();
        assert_eq!(opened.tool, "t");
    }

    #[test]
    fn tampered_state_rejected() {
        let c = codec();
        let sealed = seal(&c, &state()).unwrap();
        let mut tampered = sealed.clone();
        tampered.push('x');
        assert!(open_verified(&c, &tampered, "t", None, None).is_err());
    }

    #[test]
    fn wrong_tool_rejected() {
        let c = codec();
        let sealed = seal(&c, &state()).unwrap();
        assert!(open_verified(&c, &sealed, "other", None, None).is_err());
    }

    #[test]
    fn wrong_args_rejected() {
        let c = codec();
        let sealed = seal(&c, &state()).unwrap();
        let mut args = rmcp::model::JsonObject::new();
        args.insert("k".into(), Value::from(1));
        assert!(open_verified(&c, &sealed, "t", Some(&args), None).is_err());
    }

    #[test]
    fn wrong_principal_rejected() {
        let c = codec();
        let sealed = seal(&c, &state()).unwrap();
        let other = crate::middleware::auth::AuthContext {
            user_id: Some("other-user".to_string()),
            is_staff: false,
            is_superuser: false,
            backend: "test".to_string(),
            claims: None,
            permissions: HashSet::new(),
            cookie_csrf: false,
        };
        assert!(open_verified(&c, &sealed, "t", None, Some(&other)).is_err());
    }

    #[test]
    fn args_hash_key_order_canonical() {
        let mut a = rmcp::model::JsonObject::new();
        a.insert("b".into(), Value::from(2));
        a.insert("a".into(), Value::from(1));
        let mut b = rmcp::model::JsonObject::new();
        b.insert("a".into(), Value::from(1));
        b.insert("b".into(), Value::from(2));
        assert_eq!(args_hash(Some(&a)), args_hash(Some(&b)));
    }
}
