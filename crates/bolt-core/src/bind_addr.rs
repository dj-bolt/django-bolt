//! The address that the server binds for `runbolt --host`.
//!
//! A host is an IP address, an IPv6 address in brackets, or a host name. A
//! host that gives no address is an error. It never becomes a wildcard address.

use std::fmt::Display;
use std::io;
use std::net::{IpAddr, Ipv6Addr, SocketAddr, ToSocketAddrs};

/// Return the address to bind for `host` and `port`. The system resolver
/// resolves a host name.
pub fn resolve_bind_addr(host: &str, port: u16) -> io::Result<SocketAddr> {
    // The lookup needs a port, but `resolve_bind_addr_with` sets the port.
    resolve_bind_addr_with(host, port, |name| {
        (name, 0).to_socket_addrs().map(Iterator::collect)
    })
}

/// Return the address to bind for `host` and `port`. `resolve` gives the
/// addresses of a host name. Their ports do not matter.
///
/// An IP address binds as it is, and `resolve` does not run. Brackets hold
/// only an IPv6 address. A name binds its first IPv4 address, as Django's
/// `runserver` does. A name with no IPv4 address binds its first IPv6
/// address. The address always gets `port`.
pub fn resolve_bind_addr_with<R>(host: &str, port: u16, resolve: R) -> io::Result<SocketAddr>
where
    R: FnOnce(&str) -> io::Result<Vec<SocketAddr>>,
{
    let cannot_resolve =
        |reason: &dyn Display| io::Error::other(format!("cannot resolve host {host:?}: {reason}"));
    if let Some(inner) = host.strip_prefix('[').and_then(|h| h.strip_suffix(']')) {
        return inner
            .parse::<Ipv6Addr>()
            .map(|ip| SocketAddr::new(ip.into(), port))
            .map_err(|_| cannot_resolve(&"brackets hold only an IPv6 address"));
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(SocketAddr::new(ip, port));
    }
    let addrs = resolve(host).map_err(|e| cannot_resolve(&e))?;
    let mut addr = addrs
        .iter()
        .find(|addr| addr.is_ipv4())
        .or(addrs.first())
        .copied()
        .ok_or_else(|| cannot_resolve(&"no address"))?;
    // Keep the scope of a link-local IPv6 address. Only the port changes.
    addr.set_port(port);
    Ok(addr)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::net::{Ipv4Addr, SocketAddrV6};

    /// Run `resolve_bind_addr_with` with a fake resolver. `answer` is what the
    /// resolver gives: the addresses, or `None` for a failed lookup. Also
    /// return the name of each call of the resolver.
    fn resolve_fake(
        host: &str,
        port: u16,
        answer: Option<Vec<SocketAddr>>,
    ) -> (io::Result<SocketAddr>, Vec<String>) {
        let mut calls = Vec::new();
        let result = resolve_bind_addr_with(host, port, |name| {
            calls.push(name.to_string());
            answer.ok_or_else(|| io::Error::other("lookup failed"))
        });
        (result, calls)
    }

    fn v4(a: u8, b: u8, c: u8, d: u8, port: u16) -> SocketAddr {
        SocketAddr::new(Ipv4Addr::new(a, b, c, d).into(), port)
    }

    fn v6(ip: &str, port: u16) -> SocketAddr {
        SocketAddr::new(ip.parse::<Ipv6Addr>().unwrap().into(), port)
    }

    #[test]
    fn an_ip_address_binds_as_it_is_with_no_lookup() {
        for (host, want) in [
            ("127.0.0.1", v4(127, 0, 0, 1, 8000)),
            ("0.0.0.0", v4(0, 0, 0, 0, 8000)),
            ("::1", v6("::1", 8000)),
            ("::", v6("::", 8000)),
            ("[::1]", v6("::1", 8000)),
            ("[::]", v6("::", 8000)),
        ] {
            let (result, calls) = resolve_fake(host, 8000, Some(vec![v4(10, 0, 0, 1, 8000)]));
            assert_eq!(result.unwrap(), want, "{host}");
            assert!(calls.is_empty(), "{host}: {calls:?}");
        }
    }

    #[test]
    fn brackets_hold_only_an_ipv6_address() {
        for host in ["[localhost]", "[127.0.0.1]", "[]", "[[::1]]", "[::1%lo0]"] {
            let (result, calls) = resolve_fake(host, 8000, Some(vec![v4(127, 0, 0, 1, 8000)]));
            let err = result.unwrap_err().to_string();
            assert!(
                err.contains("brackets hold only an IPv6 address"),
                "{host}: {err}"
            );
            assert!(calls.is_empty(), "{host}: {calls:?}");
        }
    }

    #[test]
    fn a_name_binds_its_first_ipv4_address() {
        let answer = vec![
            v6("::1", 8000),
            v4(127, 0, 0, 1, 8000),
            v4(127, 0, 0, 2, 8000),
        ];
        let (result, calls) = resolve_fake("localhost", 8000, Some(answer));
        assert_eq!(result.unwrap(), v4(127, 0, 0, 1, 8000));
        assert_eq!(calls, ["localhost"]);
    }

    #[test]
    fn a_name_with_no_ipv4_address_binds_its_first_ipv6_address() {
        let answer = vec![v6("::1", 8000), v6("::2", 8000)];
        let (result, _) = resolve_fake("ip6-localhost", 8000, Some(answer));
        assert_eq!(result.unwrap(), v6("::1", 8000));
    }

    #[test]
    fn the_address_of_a_name_gets_the_port_and_keeps_its_scope() {
        let link_local = SocketAddrV6::new("fe80::1".parse().unwrap(), 9, 3, 7);
        let (result, _) = resolve_fake("link", 8000, Some(vec![link_local.into()]));
        let SocketAddr::V6(addr) = result.unwrap() else {
            panic!("an IPv6 address must stay IPv6");
        };
        assert_eq!(
            addr,
            SocketAddrV6::new("fe80::1".parse().unwrap(), 8000, 3, 7)
        );
    }

    #[test]
    fn a_name_with_no_address_is_an_error() {
        let (result, _) = resolve_fake("nothing", 8000, Some(Vec::new()));
        let err = result.unwrap_err().to_string();
        assert_eq!(err, r#"cannot resolve host "nothing": no address"#);
    }

    #[test]
    fn a_failed_lookup_is_an_error_that_names_the_host() {
        let (result, _) = resolve_fake("bad\nhost", 8000, None);
        let err = result.unwrap_err().to_string();
        assert_eq!(err, r#"cannot resolve host "bad\nhost": lookup failed"#);
    }

    #[test]
    fn the_system_resolver_fails_for_a_name_with_no_address() {
        // RFC 6761: a name under .invalid never resolves.
        let err = resolve_bind_addr("bolt-test.invalid", 8000).unwrap_err();
        let err = err.to_string();
        assert!(
            err.starts_with(r#"cannot resolve host "bolt-test.invalid": "#),
            "{err}"
        );
    }

    #[test]
    fn the_system_resolver_gives_the_ipv4_loopback_for_localhost() {
        assert_eq!(
            resolve_bind_addr("localhost", 8000).unwrap(),
            v4(127, 0, 0, 1, 8000)
        );
    }

    /// Hosts near the edges of the rules: addresses, addresses in brackets,
    /// near misses of an address, and any text.
    fn host() -> impl Strategy<Value = String> {
        prop_oneof![
            any::<IpAddr>().prop_map(|ip| ip.to_string()),
            any::<IpAddr>().prop_map(|ip| format!("[{ip}]")),
            "[\\[\\]0-9a-fA-F:.%]{0,48}",
            "(?s:.){0,40}",
        ]
    }

    /// The answer of the fake resolver: some addresses, or a failed lookup.
    fn answer() -> impl Strategy<Value = Option<Vec<SocketAddr>>> {
        proptest::option::of(proptest::collection::vec(any::<SocketAddr>(), 0..4))
    }

    proptest! {
        #![proptest_config(ProptestConfig {
            cases: ProptestConfig::default().cases.max(20_000),
            ..ProptestConfig::default()
        })]

        /// The rules of the module, for each host and each resolver answer.
        #[test]
        fn a_host_binds_only_the_address_it_names(host in host(), port in any::<u16>(), answer in answer()) {
            let (result, calls) = resolve_fake(&host, port, answer.clone());
            let bracketed = host.strip_prefix('[').and_then(|h| h.strip_suffix(']'));

            let named: Option<SocketAddr> = if let Some(inner) = bracketed {
                // An address in brackets is never looked up.
                prop_assert!(calls.is_empty());
                inner.parse::<Ipv6Addr>().ok().map(|ip| SocketAddr::new(ip.into(), port))
            } else if let Ok(ip) = host.parse::<IpAddr>() {
                // An IP address is never looked up.
                prop_assert!(calls.is_empty());
                Some(SocketAddr::new(ip, port))
            } else {
                // A name goes to the resolver once, as it is.
                prop_assert_eq!(&calls, std::slice::from_ref(&host));
                let addrs = answer.clone().unwrap_or_default();
                addrs.iter().find(|a| a.is_ipv4()).or(addrs.first()).map(|a| {
                    let mut a = *a;
                    a.set_port(port);
                    a
                })
            };

            match (&result, named) {
                (Ok(addr), Some(want)) => {
                    prop_assert_eq!(*addr, want);
                    prop_assert_eq!(addr.port(), port);
                    // A wildcard address comes only from the host or the resolver.
                    if addr.ip().is_unspecified() {
                        let from_host = host.trim_start_matches('[').trim_end_matches(']').parse::<IpAddr>().ok() == Some(addr.ip());
                        let from_resolver = answer.iter().flatten().any(|a| a.ip() == addr.ip());
                        prop_assert!(from_host || from_resolver);
                    }
                }
                (Err(err), None) => {
                    let message = err.to_string();
                    prop_assert!(message.starts_with(&format!("cannot resolve host {host:?}: ")), "{}", message);
                    // The host is escaped, so it cannot add a line to a log.
                    prop_assert!(!message.contains(['\n', '\r']), "{}", message);
                }
                (result, named) => prop_assert!(false, "{host:?}: got {result:?}, want {named:?}"),
            }
        }

        /// The system resolver does not look up an IP address.
        #[test]
        fn the_system_resolver_binds_an_ip_address_as_it_is(ip in any::<IpAddr>(), port in any::<u16>()) {
            let want = SocketAddr::new(ip, port);
            prop_assert_eq!(resolve_bind_addr(&ip.to_string(), port).unwrap(), want);
            let bracketed = resolve_bind_addr(&format!("[{ip}]"), port);
            match ip {
                IpAddr::V6(_) => prop_assert_eq!(bracketed.unwrap(), want),
                IpAddr::V4(_) => prop_assert!(bracketed.is_err()),
            }
        }
    }
}
