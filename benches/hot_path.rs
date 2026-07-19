//! Criterion micro-benchmarks for pure-Rust hot-path functions.
//!
//! These isolate per-request Rust work (query parsing, cookie parsing, type
//! coercion, path conversion) with nanosecond resolution so a change that is
//! only ~1-3% end-to-end is directly measurable here.
//!
//! Run with: `cargo bench` (or `just bench-rust`).

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use django_bolt::bench_support::{
    coerce_param, convert_path, parse_cookies_inline, parse_query_string, DEFAULT_MAX_PARAM_LENGTH,
    TYPE_BOOL, TYPE_DATETIME, TYPE_DECIMAL, TYPE_INT, TYPE_STRING, TYPE_UUID,
};

fn bench_parse_query_string(c: &mut Criterion) {
    let mut group = c.benchmark_group("parse_query_string");
    group.bench_function("empty", |b| b.iter(|| parse_query_string(black_box(""))));
    group.bench_function("single_pair", |b| {
        b.iter(|| parse_query_string(black_box("q=hello")))
    });
    group.bench_function("five_pairs_plain", |b| {
        b.iter(|| parse_query_string(black_box("a=1&b=2&c=3&d=4&e=5")))
    });
    group.bench_function("encoded_values", |b| {
        b.iter(|| parse_query_string(black_box("q=hello%20world&tag=a%2Bb&x=1")))
    });
    group.finish();
}

fn bench_parse_cookies(c: &mut Criterion) {
    let mut group = c.benchmark_group("parse_cookies_inline");
    group.bench_function("none", |b| b.iter(|| parse_cookies_inline(black_box(None))));
    group.bench_function("single", |b| {
        b.iter(|| parse_cookies_inline(black_box(Some("session=abc123"))))
    });
    group.bench_function("five_cookies", |b| {
        b.iter(|| {
            parse_cookies_inline(black_box(Some(
                "session=abc123; csrftoken=xyz; theme=dark; lang=en; tz=UTC",
            )))
        })
    });
    group.finish();
}

fn bench_coerce_param(c: &mut Criterion) {
    let mut group = c.benchmark_group("coerce_param");
    group.bench_function("int", |b| {
        b.iter(|| coerce_param(black_box("123456"), TYPE_INT, DEFAULT_MAX_PARAM_LENGTH))
    });
    group.bench_function("bool", |b| {
        b.iter(|| coerce_param(black_box("true"), TYPE_BOOL, DEFAULT_MAX_PARAM_LENGTH))
    });
    group.bench_function("string", |b| {
        b.iter(|| {
            coerce_param(
                black_box("hello-world"),
                TYPE_STRING,
                DEFAULT_MAX_PARAM_LENGTH,
            )
        })
    });
    group.bench_function("uuid", |b| {
        b.iter(|| {
            coerce_param(
                black_box("550e8400-e29b-41d4-a716-446655440000"),
                TYPE_UUID,
                DEFAULT_MAX_PARAM_LENGTH,
            )
        })
    });
    group.bench_function("datetime_rfc3339", |b| {
        b.iter(|| {
            coerce_param(
                black_box("2024-01-15T10:30:00Z"),
                TYPE_DATETIME,
                DEFAULT_MAX_PARAM_LENGTH,
            )
        })
    });
    group.bench_function("decimal", |b| {
        b.iter(|| {
            coerce_param(
                black_box("12345.6789"),
                TYPE_DECIMAL,
                DEFAULT_MAX_PARAM_LENGTH,
            )
        })
    });
    group.finish();
}

fn bench_convert_path(c: &mut Criterion) {
    let mut group = c.benchmark_group("convert_path");
    group.bench_function("static", |b| {
        b.iter(|| convert_path(black_box("/users/list")))
    });
    group.bench_function("one_param", |b| {
        b.iter(|| convert_path(black_box("/users/{user_id}")))
    });
    group.bench_function("catch_all", |b| {
        b.iter(|| convert_path(black_box("/files/{path:path}")))
    });
    group.finish();
}

criterion_group!(
    benches,
    bench_parse_query_string,
    bench_parse_cookies,
    bench_coerce_param,
    bench_convert_path
);
criterion_main!(benches);
