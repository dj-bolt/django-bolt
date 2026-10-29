//! Response metadata extraction from Python.
//!
//! This module handles the new response format where Python returns a metadata
//! tuple instead of pre-built headers. This allows Rust to:
//! - Use static content-type strings (no allocation)
//! - Lowercase header keys in a single location
//! - Serialize cookies directly without SimpleCookie overhead

use actix_web::http::header::HeaderValue;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

/// Response type for content-type determination.
/// Uses static strings to avoid allocation.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ResponseType {
    Empty,
    Json,
    Html,
    PlainText,
    OctetStream,
    Redirect,
    File,
    Streaming,
}

impl ResponseType {
    /// Parse from Python string
    #[inline]
    #[expect(
        clippy::should_implement_trait,
        reason = "infallible: an unknown name maps to OctetStream, so FromStr's Result adds nothing"
    )]
    pub fn from_str(s: &str) -> Self {
        match s {
            "empty" => Self::Empty,
            "json" => Self::Json,
            "html" => Self::Html,
            "plaintext" => Self::PlainText,
            "redirect" => Self::Redirect,
            "file" => Self::File,
            "streaming" => Self::Streaming,
            _ => Self::OctetStream,
        }
    }

    /// Get static content-type string (no allocation)
    #[inline]
    pub const fn content_type(&self) -> &'static str {
        match self {
            Self::Empty => "",
            Self::Json => "application/json",
            Self::Html => "text/html; charset=utf-8",
            Self::PlainText => "text/plain; charset=utf-8",
            Self::OctetStream => "application/octet-stream",
            Self::Redirect => "",
            Self::File => "",
            Self::Streaming => "",
        }
    }

    /// The Content-Type header value, or None for a type with no content type.
    /// The values are constants, so a response copies and checks no bytes.
    #[inline]
    pub fn content_type_header(&self) -> Option<HeaderValue> {
        const fn header(response_type: ResponseType) -> Option<HeaderValue> {
            let content_type = response_type.content_type();
            if content_type.is_empty() {
                None
            } else {
                Some(HeaderValue::from_static(content_type))
            }
        }
        const EMPTY: Option<HeaderValue> = header(ResponseType::Empty);
        const JSON: Option<HeaderValue> = header(ResponseType::Json);
        const HTML: Option<HeaderValue> = header(ResponseType::Html);
        const PLAIN_TEXT: Option<HeaderValue> = header(ResponseType::PlainText);
        const OCTET_STREAM: Option<HeaderValue> = header(ResponseType::OctetStream);
        const REDIRECT: Option<HeaderValue> = header(ResponseType::Redirect);
        const FILE: Option<HeaderValue> = header(ResponseType::File);
        const STREAMING: Option<HeaderValue> = header(ResponseType::Streaming);
        match self {
            Self::Empty => EMPTY,
            Self::Json => JSON,
            Self::Html => HTML,
            Self::PlainText => PLAIN_TEXT,
            Self::OctetStream => OCTET_STREAM,
            Self::Redirect => REDIRECT,
            Self::File => FILE,
            Self::Streaming => STREAMING,
        }
    }
}

/// Raw cookie data from Python
#[derive(Debug)]
pub struct CookieData {
    pub name: String,
    pub value: String,
    pub path: String,
    pub max_age: Option<i64>,
    pub expires: Option<String>,
    pub domain: Option<String>,
    pub secure: bool,
    pub httponly: bool,
    pub samesite: Option<String>,
}

/// Response metadata for Rust-side header building
#[derive(Debug)]
pub struct ResponseMeta {
    pub response_type: ResponseType,
    pub custom_content_type: Option<String>,
    pub custom_headers: Option<Vec<(String, String)>>,
    pub cookies: Option<Vec<CookieData>>,
}

/// Pre-built static ResponseMeta for common response types.
/// Integer tags from Python map directly to these constants,
/// avoiding String allocation + tuple parsing per response.
/// Must match _RESPONSE_META_* constants in serialization.py.
pub static STATIC_META_JSON: ResponseMeta = ResponseMeta {
    response_type: ResponseType::Json,
    custom_content_type: None,
    custom_headers: None,
    cookies: None,
};

pub static STATIC_META_PLAINTEXT: ResponseMeta = ResponseMeta {
    response_type: ResponseType::PlainText,
    custom_content_type: None,
    custom_headers: None,
    cookies: None,
};

pub static STATIC_META_OCTETSTREAM: ResponseMeta = ResponseMeta {
    response_type: ResponseType::OctetStream,
    custom_content_type: None,
    custom_headers: None,
    cookies: None,
};

pub static STATIC_META_EMPTY: ResponseMeta = ResponseMeta {
    response_type: ResponseType::Empty,
    custom_content_type: None,
    custom_headers: None,
    cookies: None,
};

pub static STATIC_META_HTML: ResponseMeta = ResponseMeta {
    response_type: ResponseType::Html,
    custom_content_type: None,
    custom_headers: None,
    cookies: None,
};

impl ResponseMeta {
    /// Fast-path: resolve integer meta tag to static ResponseMeta.
    /// Returns None if the tag is not a known constant.
    #[inline]
    pub fn from_tag(tag: u8) -> Option<&'static ResponseMeta> {
        match tag {
            0 => Some(&STATIC_META_JSON),
            1 => Some(&STATIC_META_PLAINTEXT),
            2 => Some(&STATIC_META_OCTETSTREAM),
            3 => Some(&STATIC_META_EMPTY),
            4 => Some(&STATIC_META_HTML),
            _ => None,
        }
    }

    /// Extract from Python tuple: (response_type, custom_ct, headers, cookies)
    pub fn from_python(obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        let tuple = obj.cast::<PyTuple>()?;

        // Element 0: response_type string
        let type_str: String = tuple.get_item(0)?.extract()?;
        let response_type = ResponseType::from_str(&type_str);

        // Element 1: custom content-type (None or string)
        let custom_content_type: Option<String> = tuple.get_item(1)?.extract()?;

        // Element 2: custom headers (None or list of tuples)
        let custom_headers: Option<Vec<(String, String)>> = tuple.get_item(2)?.extract()?;

        // Element 3: cookies (None or list of 9-tuples)
        #[expect(
            clippy::type_complexity,
            reason = "PyO3 wire format: ResponseWireV1 sends each cookie as one 9-tuple"
        )]
        let cookies_raw: Option<
            Vec<(
                String,         // name
                String,         // value
                String,         // path
                Option<i64>,    // max_age
                Option<String>, // expires
                Option<String>, // domain
                bool,           // secure
                bool,           // httponly
                Option<String>, // samesite
            )>,
        > = tuple.get_item(3)?.extract()?;

        let cookies = cookies_raw.map(|vec| {
            vec.into_iter()
                .map(
                    |(name, value, path, max_age, expires, domain, secure, httponly, samesite)| {
                        CookieData {
                            name,
                            value,
                            path,
                            max_age,
                            expires,
                            domain,
                            secure,
                            httponly,
                            samesite,
                        }
                    },
                )
                .collect()
        });

        Ok(ResponseMeta {
            response_type,
            custom_content_type,
            custom_headers,
            cookies,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_response_type_from_str() {
        assert_eq!(ResponseType::from_str("empty"), ResponseType::Empty);
        assert_eq!(ResponseType::from_str("json"), ResponseType::Json);
        assert_eq!(ResponseType::from_str("html"), ResponseType::Html);
        assert_eq!(ResponseType::from_str("plaintext"), ResponseType::PlainText);
        assert_eq!(ResponseType::from_str("redirect"), ResponseType::Redirect);
        assert_eq!(ResponseType::from_str("file"), ResponseType::File);
        assert_eq!(ResponseType::from_str("streaming"), ResponseType::Streaming);
        assert_eq!(ResponseType::from_str("unknown"), ResponseType::OctetStream);
    }

    #[test]
    fn test_response_type_content_type() {
        assert_eq!(ResponseType::Empty.content_type(), "");
        assert_eq!(ResponseType::Json.content_type(), "application/json");
        assert_eq!(
            ResponseType::Html.content_type(),
            "text/html; charset=utf-8"
        );
        assert_eq!(
            ResponseType::PlainText.content_type(),
            "text/plain; charset=utf-8"
        );
        assert_eq!(
            ResponseType::OctetStream.content_type(),
            "application/octet-stream"
        );
        assert_eq!(ResponseType::Redirect.content_type(), "");
        assert_eq!(ResponseType::File.content_type(), "");
        assert_eq!(ResponseType::Streaming.content_type(), "");
    }

    #[test]
    fn test_response_type_content_type_header_matches_content_type() {
        for response_type in [
            ResponseType::Empty,
            ResponseType::Json,
            ResponseType::Html,
            ResponseType::PlainText,
            ResponseType::OctetStream,
            ResponseType::Redirect,
            ResponseType::File,
            ResponseType::Streaming,
        ] {
            let content_type = response_type.content_type();
            let expected =
                (!content_type.is_empty()).then(|| HeaderValue::from_static(content_type));
            assert_eq!(
                response_type.content_type_header(),
                expected,
                "{response_type:?}"
            );
        }
    }
}
