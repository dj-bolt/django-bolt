//! A `--host` binds only the address that it names, and never a wildcard
//! address that it does not name. A fake resolver gives the addresses of a
//! name, so no input goes to DNS.
#![no_main]

use bolt_core::bind_addr::{resolve_bind_addr, resolve_bind_addr_with};
use libfuzzer_sys::fuzz_target;
use std::io;
use std::net::{IpAddr, Ipv6Addr, SocketAddr};

fuzz_target!(|input: (&str, u16, Option<Vec<SocketAddr>>)| {
    let (host, port, answer) = input;
    let mut calls = Vec::new();
    let result = resolve_bind_addr_with(host, port, |name| {
        calls.push(name.to_string());
        answer
            .clone()
            .ok_or_else(|| io::Error::other("lookup failed"))
    });

    let bracketed = host.strip_prefix('[').and_then(|h| h.strip_suffix(']'));
    let literal = host.parse::<IpAddr>().ok();
    let named = if let Some(inner) = bracketed {
        assert!(calls.is_empty(), "{host:?} in brackets was looked up");
        inner
            .parse::<Ipv6Addr>()
            .ok()
            .map(|ip| SocketAddr::new(ip.into(), port))
    } else if let Some(ip) = literal {
        assert!(calls.is_empty(), "the address {host:?} was looked up");
        Some(SocketAddr::new(ip, port))
    } else {
        assert_eq!(calls, [host]);
        let addrs = answer.clone().unwrap_or_default();
        addrs
            .iter()
            .find(|a| a.is_ipv4())
            .or(addrs.first())
            .map(|a| {
                let mut a = *a;
                a.set_port(port);
                a
            })
    };

    match (&result, named) {
        (Ok(addr), Some(want)) => {
            assert_eq!(*addr, want, "{host:?}");
            assert_eq!(addr.port(), port);
            if addr.ip().is_unspecified() {
                let from_host = bracketed.unwrap_or(host).parse::<IpAddr>().ok() == Some(addr.ip());
                let from_resolver = answer.iter().flatten().any(|a| a.ip() == addr.ip());
                assert!(
                    from_host || from_resolver,
                    "{host:?} gave the wildcard {addr}"
                );
            }
        }
        (Err(err), None) => {
            let message = err.to_string();
            assert!(
                message.starts_with(&format!("cannot resolve host {host:?}: ")),
                "{message}"
            );
            assert!(!message.contains(['\n', '\r']), "{message}");
        }
        (result, named) => panic!("{host:?}: got {result:?}, want {named:?}"),
    }

    // The system resolver must agree on each host that needs no lookup. The
    // checks above show that these hosts never reach the resolver.
    if bracketed.is_some() || literal.is_some() {
        assert_eq!(resolve_bind_addr(host, port).ok(), result.ok(), "{host:?}");
    }
});
