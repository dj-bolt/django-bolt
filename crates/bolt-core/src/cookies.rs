//! Cookie serialization using the `cookie` crate for RFC 6265 compliance.
//!
//! Uses the battle-tested `cookie` crate instead of manual string building
//! to ensure proper validation, escaping, and security.
//!
//! Cookie values are quoted and read as Django quotes and reads them, so a
//! cookie that Django sets reads back in Bolt, and the reverse.

use std::borrow::Cow;
use std::fmt::Write;

use actix_web::cookie::{time, Cookie, SameSite};

use crate::response_meta::CookieData;

/// Format a CookieData into a Set-Cookie header value.
///
/// Uses the `cookie` crate for RFC 6265 compliant serialization:
/// - Validates cookie names (rejects invalid characters)
/// - Rejects control characters in the value, as Django does
/// - Rejects a value character above U+00FF, which Django cannot send
/// - Quotes the value as Django does (see `quote_cookie_value`)
///
/// Returns None if the cookie name or value is invalid, with a warning logged.
#[inline]
pub fn format_cookie(c: &CookieData) -> Option<String> {
    // Validate cookie name - reject if it contains invalid characters
    // RFC 6265: cookie-name = token (no CTLs, separators, or whitespace)
    if !is_valid_cookie_name(&c.name) {
        eprintln!(
            "[django-bolt] WARNING: Invalid cookie name '{}' - contains invalid characters",
            c.name
        );
        return None;
    }

    // Reject control characters, as Python `http.cookies` does for Django.
    if contains_control_chars(&c.value) {
        eprintln!(
            "[django-bolt] WARNING: Cookie '{}' value contains control characters - rejected for security",
            c.name
        );
        return None;
    }

    // Reject a character above U+00FF. Django cannot send it, because it encodes
    // a header as Latin-1. A browser that sends it back makes the `Cookie` header
    // unreadable, so the request gets no cookies.
    if c.value.chars().any(|ch| ch > '\u{ff}') {
        eprintln!(
            "[django-bolt] WARNING: Cookie '{}' value contains a character above U+00FF - rejected",
            c.name
        );
        return None;
    }

    // Build cookie using the cookie crate
    let mut cookie = Cookie::build(&c.name, quote_cookie_value(&c.value)).path(&c.path);

    // Max-Age (validate non-negative)
    if let Some(max_age) = c.max_age {
        if max_age < 0 {
            eprintln!(
                "[django-bolt] WARNING: Cookie '{}' has negative max_age ({}) - using 0",
                c.name, max_age
            );
            cookie = cookie.max_age(time::Duration::ZERO);
        } else {
            cookie = cookie.max_age(time::Duration::seconds(max_age));
        }
    }

    // Expires is handled manually below (cookie crate expects OffsetDateTime,
    // but we receive a pre-formatted string from Python)

    // Domain
    if let Some(ref domain) = c.domain {
        cookie = cookie.domain(domain);
    }

    // Secure
    if c.secure {
        cookie = cookie.secure(true);
    }

    // HttpOnly
    if c.httponly {
        cookie = cookie.http_only(true);
    }

    // SameSite
    if let Some(ref samesite) = c.samesite {
        let ss = match samesite.to_lowercase().as_str() {
            "strict" => SameSite::Strict,
            "lax" => SameSite::Lax,
            "none" => SameSite::None,
            _ => SameSite::Lax, // Default to Lax for invalid values
        };
        cookie = cookie.same_site(ss);
    }

    let built = cookie.finish();
    let mut result = built.to_string();

    // Append Expires manually if provided (cookie crate requires OffsetDateTime)
    if let Some(ref expires) = c.expires {
        result.push_str("; Expires=");
        result.push_str(expires);
    }

    Some(result)
}

/// Validate cookie name per RFC 6265.
/// Cookie names must be tokens: no CTLs, separators, or whitespace.
#[inline]
fn is_valid_cookie_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }

    // RFC 6265 token characters (subset of ASCII)
    // Disallowed: CTLs (0-31, 127), separators: ()<>@,;:\"/[]?={} SP HT
    name.bytes().all(|b| {
        b > 32
            && b < 127
            && !matches!(
                b,
                b'(' | b')'
                    | b'<'
                    | b'>'
                    | b'@'
                    | b','
                    | b';'
                    | b':'
                    | b'\\'
                    | b'"'
                    | b'/'
                    | b'['
                    | b']'
                    | b'?'
                    | b'='
                    | b'{'
                    | b'}'
            )
    })
}

/// Check if a string contains control characters that could enable header injection.
#[inline]
fn contains_control_chars(value: &str) -> bool {
    value.bytes().any(|b| b < 32 || b == 127)
}

/// Characters that a cookie value can hold without quotes
/// (Python `http.cookies._LegalChars`).
#[inline]
fn is_legal_cookie_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || "!#$%&'*+-.^_`|~:".contains(c)
}

/// Quote a cookie value as Django `set_cookie` does (Python `http.cookies._quote`).
///
/// A value of legal characters only stays as it is. Any other value is put in
/// double quotes. In the quotes, `"` and `\` get a backslash, and a Latin-1
/// character that is not safe becomes an octal escape (`;` gives `\073`).
/// Thus a value cannot end the cookie or add an attribute.
pub fn quote_cookie_value(value: &str) -> Cow<'_, str> {
    if !value.is_empty() && value.chars().all(is_legal_cookie_char) {
        return Cow::Borrowed(value);
    }
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('"');
    for c in value.chars() {
        match c {
            '"' => quoted.push_str("\\\""),
            '\\' => quoted.push_str("\\\\"),
            c if is_legal_cookie_char(c) || " ()/<=>?@[]{}".contains(c) => quoted.push(c),
            c if u32::from(c) < 256 => {
                write!(quoted, "\\{:03o}", u32::from(c)).expect("a String write cannot fail");
            }
            c => quoted.push(c),
        }
    }
    quoted.push('"');
    Cow::Owned(quoted)
}

/// Remove the quotes of a cookie value as Django does (Python `http.cookies._unquote`).
///
/// A value in double quotes loses them, and its escapes are decoded:
/// `\ooo` gives the character of that octal code, and `\c` gives `c`.
/// Any other value stays as it is.
pub fn unquote_cookie_value(value: &str) -> Cow<'_, str> {
    let Some(inner) = value
        .strip_prefix('"')
        .and_then(|rest| rest.strip_suffix('"'))
    else {
        return Cow::Borrowed(value);
    };
    if !inner.contains('\\') {
        return Cow::Borrowed(inner);
    }
    let mut unquoted = String::with_capacity(inner.len());
    let mut rest = inner;
    while let Some(pos) = rest.find('\\') {
        unquoted.push_str(&rest[..pos]);
        let after = &rest[pos + 1..];
        match after.as_bytes() {
            [high @ b'0'..=b'3', mid @ b'0'..=b'7', low @ b'0'..=b'7', ..] => {
                let code = u32::from(high - b'0') << 6
                    | u32::from(mid - b'0') << 3
                    | u32::from(low - b'0');
                unquoted.push(char::from_u32(code).expect("an octal escape is below 256"));
                rest = &after[3..];
            }
            // Python matches any character after the backslash, except a newline.
            _ => match after.chars().next().filter(|&c| c != '\n') {
                Some(c) => {
                    unquoted.push(c);
                    rest = &after[c.len_utf8()..];
                }
                None => {
                    unquoted.push('\\');
                    rest = after;
                }
            },
        }
    }
    unquoted.push_str(rest);
    Cow::Owned(unquoted)
}

/// The (name, value) pairs of a `Cookie` header, in their order, as Django
/// `parse_cookie` reads them.
///
/// Pairs are split at `;`, and each pair at its first `=`. A pair with no `=`
/// is a value with an empty name. The name and the value lose their outer
/// whitespace, and the value loses its quotes. A pair that is empty is skipped.
/// A name can occur two times. Django keeps the last value.
pub fn cookie_pairs(header: &str) -> impl Iterator<Item = (&str, Cow<'_, str>)> {
    header.split(';').filter_map(|pair| {
        let (name, value) = pair.split_once('=').unwrap_or(("", pair));
        let (name, value) = (name.trim(), value.trim());
        (!name.is_empty() || !value.is_empty()).then(|| (name, unquote_cookie_value(value)))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// Latin-1 text with no control character: `format_cookie` refuses the rest.
    const COOKIE_TEXT: &str = "[^\\x00-\\x1f\\x7f\\x{100}-\\x{10ffff}]*";
    /// An RFC 6265 cookie name (a token).
    const COOKIE_NAME: &str = "[!#$%&'*+.^_`|~0-9A-Za-z-]{1,12}";

    proptest! {
        #[test]
        fn a_quoted_value_unquotes_to_itself(value in any::<String>()) {
            let quoted = quote_cookie_value(&value);
            prop_assert_eq!(unquote_cookie_value(&quoted), value.as_str());
        }

        /// A quoted value has no `;`, `,` or control character, so it cannot
        /// end the cookie, and its only bare quotes are the outer two.
        #[test]
        fn a_quoted_value_stays_one_value(value in COOKIE_TEXT) {
            let quoted = quote_cookie_value(&value);
            // ASCII only, so the browser sends back a header that Bolt can read.
            prop_assert!(quoted.is_ascii());
            prop_assert!(!quoted.contains([';', ',']));
            prop_assert!(!quoted.bytes().any(|byte| byte < 32 || byte == 127));
            let inner = quoted.strip_prefix('"').and_then(|rest| rest.strip_suffix('"')).unwrap_or(&quoted);
            prop_assert!(!inner.replace("\\\\", "").replace("\\\"", "").contains('"'));
        }

        #[test]
        fn set_cookies_read_back(cookies in prop::collection::vec((COOKIE_NAME, COOKIE_TEXT), 0..6)) {
            let header = cookies
                .iter()
                .map(|(name, value)| format!("{name}={}", quote_cookie_value(value)))
                .collect::<Vec<_>>()
                .join("; ");
            let read: Vec<(String, String)> = cookie_pairs(&header)
                .map(|(name, value)| (name.to_owned(), value.into_owned()))
                .collect();
            // Outer spaces of a value stay: they are in the quotes.
            prop_assert_eq!(read, cookies);
        }
    }

    #[test]
    fn test_simple_cookie() {
        let c = CookieData {
            name: "session".to_string(),
            value: "abc123".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("session=abc123"));
        assert!(result.contains("Path=/"));
    }

    #[test]
    fn test_full_cookie() {
        let c = CookieData {
            name: "auth".to_string(),
            value: "token".to_string(),
            path: "/app".to_string(),
            max_age: Some(3600),
            expires: Some("Thu, 01 Jan 2025 00:00:00 GMT".to_string()),
            domain: Some("example.com".to_string()),
            secure: true,
            httponly: true,
            samesite: Some("Strict".to_string()),
        };
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("auth=token"));
        assert!(result.contains("Path=/app"));
        assert!(result.contains("Max-Age=3600"));
        assert!(result.contains("Domain=example.com"));
        assert!(result.contains("Secure"));
        assert!(result.contains("HttpOnly"));
        assert!(result.contains("SameSite=Strict"));
        assert!(result.contains("Expires=Thu, 01 Jan 2025 00:00:00 GMT"));
    }

    #[test]
    fn test_value_with_spaces() {
        let c = CookieData {
            name: "data".to_string(),
            value: "hello world".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        // Cookie crate handles quoting/encoding
        let result = format_cookie(&c).unwrap();
        assert!(result.contains("data="));
    }

    #[test]
    fn test_invalid_cookie_name_rejected() {
        let c = CookieData {
            name: "session; Path=/evil".to_string(), // Injection attempt
            value: "xyz".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        assert!(format_cookie(&c).is_none());
    }

    fn cookie_with_value(value: &str) -> CookieData {
        CookieData {
            name: "session".to_string(),
            value: value.to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        }
    }

    #[test]
    fn test_control_chars_rejected() {
        // Header injection attempt
        assert!(format_cookie(&cookie_with_value("value\r\nSet-Cookie: evil=1")).is_none());
        assert!(format_cookie(&cookie_with_value("del\x7f")).is_none());
    }

    /// Django cannot send a character above U+00FF in a header. A browser that
    /// sends it back makes the `Cookie` header unreadable for each later request.
    #[test]
    fn a_value_above_latin1_is_rejected() {
        assert!(format_cookie(&cookie_with_value("日本")).is_none());
        assert!(format_cookie(&cookie_with_value("a\u{100}")).is_none());
        assert_eq!(
            format_cookie(&cookie_with_value("\u{ff}")).unwrap(),
            r#"session="\377"; Path=/"#
        );
    }

    #[test]
    fn a_value_cannot_add_an_attribute() {
        let result = format_cookie(&cookie_with_value("x; Domain=evil.example")).unwrap();
        assert!(
            result.starts_with(r#"session="x\073 Domain=evil.example"; "#),
            "{result}"
        );
    }

    #[test]
    fn values_are_quoted_as_django_quotes_them() {
        for (value, quoted) in [
            ("abc123", "abc123"),
            ("", r#""""#),
            ("a b", r#""a b""#),
            (r#"say "hi""#, r#""say \"hi\"""#),
            (r"back\slash", r#""back\\slash""#),
            ("café", r#""caf\351""#),
            ("a,b", r#""a\054b""#),
        ] {
            assert_eq!(quote_cookie_value(value), quoted, "{value}");
            assert_eq!(unquote_cookie_value(quoted), value, "{quoted}");
        }
    }

    #[test]
    fn unquote_keeps_what_python_keeps() {
        for (value, unquoted) in [
            (r#"""#, r#"""#),
            (r#""abc"#, r#""abc"#),
            (r#""a\"#, r#""a\"#),
            (r#""\x""#, "x"),
            (r#""\400""#, "400"),
            ("\"a\\\nb\"", "a\\\nb"),
        ] {
            assert_eq!(unquote_cookie_value(value), unquoted, "{value}");
        }
    }

    #[test]
    fn cookie_pairs_follow_django_parse_cookie() {
        let pairs: Vec<(&str, String)> = cookie_pairs(r#" a = 1 ;b="x y"; ;novalue; c=; =d"#)
            .map(|(name, value)| (name, value.into_owned()))
            .collect();
        assert_eq!(
            pairs,
            [
                ("a", "1".to_string()),
                ("b", "x y".to_string()),
                ("", "novalue".to_string()),
                ("c", String::new()),
                ("", "d".to_string()),
            ]
        );
    }

    #[test]
    fn test_empty_name_rejected() {
        let c = CookieData {
            name: "".to_string(),
            value: "value".to_string(),
            path: "/".to_string(),
            max_age: None,
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        assert!(format_cookie(&c).is_none());
    }

    #[test]
    fn test_negative_max_age_handled() {
        let c = CookieData {
            name: "session".to_string(),
            value: "abc".to_string(),
            path: "/".to_string(),
            max_age: Some(-100), // Invalid negative
            expires: None,
            domain: None,
            secure: false,
            httponly: false,
            samesite: None,
        };
        let result = format_cookie(&c).unwrap();
        // Should use 0 instead of negative
        assert!(result.contains("Max-Age=0"));
    }

    #[test]
    fn test_samesite_values() {
        for (input, expected) in [("Strict", "Strict"), ("Lax", "Lax"), ("None", "None")] {
            let c = CookieData {
                name: "test".to_string(),
                value: "val".to_string(),
                path: "/".to_string(),
                max_age: None,
                expires: None,
                domain: None,
                secure: false,
                httponly: false,
                samesite: Some(input.to_string()),
            };
            let result = format_cookie(&c).unwrap();
            assert!(
                result.contains(&format!("SameSite={}", expected)),
                "Expected SameSite={} in {}",
                expected,
                result
            );
        }
    }
}
