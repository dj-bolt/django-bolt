//! Shared HTTP core for django-bolt.
//!
//! Everything below the dispatch layer lives here: the `PyRequest` type,
//! shared server state, routing, parameter coercion, auth/guards, the
//! Rust-side middleware pipeline, response building and streaming.

pub mod cookies;
pub mod cors;
pub mod error;
pub mod form_parsing;
pub mod json;
pub mod metadata;
pub mod middleware;
pub mod permissions;
pub mod request;
pub mod request_pipeline;
pub mod response_builder;
pub mod response_meta;
pub mod responses;
pub mod router;
pub mod state;
pub mod static_files;
pub mod streaming;
pub mod streaming_compression;
pub mod type_coercion;
pub mod validation;
