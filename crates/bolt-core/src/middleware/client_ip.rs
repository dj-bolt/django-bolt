//! Client address resolution for rate limiting.
//!
//! A client sends `X-Forwarded-For` itself. Each proxy appends to it. The
//! leftmost entry is therefore the value the client chose, not a fact about the
//! connection. Bolt trusts a forwarding header only when a declared proxy sent
//! it.
//!
//! `settings.BOLT_TRUSTED_PROXIES` declares those proxies as a CIDR list. The
//! list is empty by default. With an empty list Bolt ignores every forwarding
//! header and keys on the peer address.

use actix_web::http::header::HeaderMap;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::net::{IpAddr, SocketAddr};

/// One entry of the trusted proxy list.
///
/// A bare address such as `127.0.0.1` gets the full prefix length, so it
/// matches only itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IpCidr {
    network: IpAddr,
    prefix_len: u8,
}

impl IpCidr {
    /// Parse one entry, for example `10.0.0.0/8`, `127.0.0.1` or `::1`.
    ///
    /// Returns an error message that names the rejected entry. The caller turns
    /// it into a startup failure.
    pub fn parse(value: &str) -> Result<Self, String> {
        let entry = value.trim();
        let (address, prefix) = match entry.split_once('/') {
            Some((address, prefix)) => (address, Some(prefix)),
            None => (entry, None),
        };

        let network = address
            .parse::<IpAddr>()
            .map_err(|_| format!("{entry:?} is not an IP address or CIDR block"))?;
        let max_prefix_len = if network.is_ipv4() { 32 } else { 128 };

        let prefix_len = match prefix {
            Some(prefix) => prefix
                .parse::<u8>()
                .map_err(|_| format!("{entry:?} has a prefix length that is not a number"))?,
            None => max_prefix_len,
        };
        if prefix_len > max_prefix_len {
            return Err(format!(
                "{entry:?} has prefix length {prefix_len}, above the maximum of {max_prefix_len}"
            ));
        }

        // `::ffff:a.b.c.d/n` with n >= 96 is an IPv4 block. Peers are
        // normalized to IPv4, so store it as one or it never matches.
        if let IpAddr::V6(v6) = network {
            if prefix_len >= 96 {
                if let Some(v4) = v6.to_ipv4_mapped() {
                    return Ok(IpCidr {
                        network: IpAddr::V4(v4),
                        prefix_len: prefix_len - 96,
                    });
                }
            }
        }

        Ok(IpCidr {
            network,
            prefix_len,
        })
    }

    /// Report whether `candidate` is inside this block.
    ///
    /// An IPv4 block matches an IPv4-mapped IPv6 peer. An IPv6 block that is
    /// wider than the mapped range stays IPv6 and never matches IPv4.
    pub fn contains(&self, candidate: &IpAddr) -> bool {
        match self.network {
            IpAddr::V4(network) => match normalize(*candidate) {
                IpAddr::V4(candidate) => {
                    prefix_eq(&network.octets(), &candidate.octets(), self.prefix_len)
                }
                IpAddr::V6(_) => false,
            },
            IpAddr::V6(network) => match candidate {
                IpAddr::V6(candidate) => {
                    prefix_eq(&network.octets(), &candidate.octets(), self.prefix_len)
                }
                IpAddr::V4(_) => false,
            },
        }
    }
}

/// Deployment-wide proxy trust policy, parsed once for one server or test app.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TrustedProxies {
    blocks: Vec<IpCidr>,
}

impl TrustedProxies {
    pub fn parse(entries: &[String]) -> Result<Self, String> {
        let blocks = entries
            .iter()
            .map(|entry| IpCidr::parse(entry))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self { blocks })
    }

    /// Preserve Python's `ImproperlyConfigured` validation while building the
    /// Rust hot-path representation only once at application startup.
    pub fn from_django_settings(py: Python<'_>) -> PyResult<Self> {
        let entries: Vec<String> = py
            .import("django_bolt.middleware.compiler")?
            .getattr("get_trusted_proxies")?
            .call0()?
            .extract()?;
        Self::parse(&entries).map_err(|error| {
            PyValueError::new_err(format!("Invalid BOLT_TRUSTED_PROXIES entry: {error}"))
        })
    }

    #[inline]
    fn is_empty(&self) -> bool {
        self.blocks.is_empty()
    }

    #[inline]
    fn contains(&self, address: &IpAddr) -> bool {
        self.blocks.iter().any(|block| block.contains(address))
    }
}

/// Compare the first `prefix_len` bits of two addresses.
fn prefix_eq(network: &[u8], candidate: &[u8], prefix_len: u8) -> bool {
    let whole_bytes = (prefix_len / 8) as usize;
    if network[..whole_bytes] != candidate[..whole_bytes] {
        return false;
    }

    let leftover_bits = prefix_len % 8;
    if leftover_bits == 0 {
        return true;
    }

    let mask = 0xffu8 << (8 - leftover_bits);
    network[whole_bytes] & mask == candidate[whole_bytes] & mask
}

/// Map an IPv4-mapped IPv6 address back to IPv4.
///
/// A dual-stack listener reports an IPv4 peer as `::ffff:a.b.c.d`. Without this
/// step the same client gets two different rate limit buckets.
fn normalize(address: IpAddr) -> IpAddr {
    match address {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => IpAddr::V4(v4),
            None => IpAddr::V6(v6),
        },
        v4 => v4,
    }
}

/// Parse one address and normalize it. Returns `None` for anything else.
fn parse_ip(value: &str) -> Option<IpAddr> {
    value.trim().parse::<IpAddr>().ok().map(normalize)
}

/// Parse a forwarding hop. Plain IPs are standard; `SocketAddr` additionally
/// accepts proxy output such as `192.0.2.1:1234` and `[2001:db8::1]:443`.
fn parse_forwarded_ip(value: &str) -> Option<IpAddr> {
    let value = value.trim();
    parse_ip(value).or_else(|| {
        value
            .parse::<SocketAddr>()
            .ok()
            .map(|address| normalize(address.ip()))
    })
}

/// Resolve the canonical client address used by rate limiting, Django request
/// metadata, and request logging.
///
/// The rules are:
///
/// 1. With no trusted proxy declared, use the peer address. Forwarding headers
///    are client input, so ignore them.
/// 2. With trusted proxies declared but a peer outside the list, use the peer
///    address. That peer is not a declared proxy, so its headers prove nothing.
/// 3. With a trusted peer, read `X-Forwarded-For` from the right. Return the
///    first entry that is not a trusted proxy. That entry is the client.
/// 4. When every `X-Forwarded-For` entry is trusted, return the leftmost entry.
/// 5. Fall back to a valid `X-Real-IP`, then the peer address.
/// 6. A malformed forwarding chain falls back to the peer; it never exposes an
///    attacker-controlled entry farther left.
///
/// Returns `None` only when there is no peer address and no usable header. The
/// caller then keys on a constant.
///
/// `forwarded_for` yields each `X-Forwarded-For` header value in wire order.
/// A `None` item is a value that is not valid text. `resolve` reads the values
/// lazily and from the right, so a request that reaches rule 1 or 2 never
/// touches them.
pub fn resolve<'a>(
    forwarded_for: impl DoubleEndedIterator<Item = Option<&'a str>>,
    real_ip: Option<Option<&'a str>>,
    peer_addr: Option<IpAddr>,
    trusted_proxies: &TrustedProxies,
) -> Option<IpAddr> {
    let peer = peer_addr.map(normalize);

    if trusted_proxies.is_empty() {
        return peer;
    }
    match peer {
        Some(address) if trusted_proxies.contains(&address) => {}
        _ => return peer,
    }

    match client_from_forwarded_for(forwarded_for, trusted_proxies) {
        Ok(Some(client)) => return Some(client),
        Ok(None) => {}
        // A malformed chain proves nothing. Fall back to the socket peer
        // instead of searching farther left through client-controlled data.
        Err(()) => return peer,
    }
    if let Some(value) = real_ip {
        return value.and_then(parse_forwarded_ip).or(peer);
    }

    peer
}

/// Resolve from an Actix header map. Duplicate `X-Forwarded-For` headers are
/// read in order without a merge step.
#[inline]
pub fn resolve_from_headers(
    headers: &HeaderMap,
    peer_addr: Option<IpAddr>,
    trusted_proxies: &TrustedProxies,
) -> Option<IpAddr> {
    resolve(
        headers
            .get_all("x-forwarded-for")
            .map(|value| value.to_str().ok()),
        headers.get("x-real-ip").map(|value| value.to_str().ok()),
        peer_addr,
        trusted_proxies,
    )
}

/// Find the client across the `X-Forwarded-For` values.
///
/// The rightmost entry that is not a trusted proxy is the client. A malformed
/// hop invalidates the chain; skipping it could expose a spoofed entry farther
/// left when a proxy emits an unsupported representation of the real client.
fn client_from_forwarded_for<'a>(
    values: impl DoubleEndedIterator<Item = Option<&'a str>>,
    trusted_proxies: &TrustedProxies,
) -> Result<Option<IpAddr>, ()> {
    let mut leftmost: Option<IpAddr> = None;

    for value in values.rev() {
        let Some(value) = value else {
            return Err(());
        };
        for entry in value.rsplit(',') {
            let Some(address) = parse_forwarded_ip(entry) else {
                return Err(());
            };
            leftmost = Some(address);
            if !trusted_proxies.contains(&address) {
                return Ok(Some(address));
            }
        }
    }

    Ok(leftmost)
}

#[cfg(test)]
mod tests {
    use super::*;
    use actix_web::http::header::{HeaderName, HeaderValue};

    // Keep expectations readable while the production API stays allocation-free.
    fn resolve(
        headers: &HeaderMap,
        peer_addr: Option<&str>,
        trusted_proxies: &TrustedProxies,
    ) -> Option<String> {
        resolve_from_headers(headers, peer_addr.and_then(parse_ip), trusted_proxies)
            .map(|address| address.to_string())
    }

    fn proxies(entries: &[&str]) -> TrustedProxies {
        TrustedProxies::parse(
            &entries
                .iter()
                .map(|entry| entry.to_string())
                .collect::<Vec<_>>(),
        )
        .unwrap()
    }

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.append(
                HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        map
    }

    #[test]
    fn reads_duplicate_forwarded_for_headers_in_order() {
        let resolved = resolve(
            &headers(&[
                ("x-forwarded-for", "1.1.1.1, 203.0.113.9"),
                ("x-forwarded-for", "10.0.0.1"),
            ]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn parses_blocks_and_bare_addresses() {
        assert!(IpCidr::parse("10.0.0.0/8").is_ok());
        assert!(IpCidr::parse(" 127.0.0.1 ").is_ok());
        assert!(IpCidr::parse("::1").is_ok());
        assert!(IpCidr::parse("2001:db8::/32").is_ok());
    }

    #[test]
    fn rejects_malformed_entries() {
        assert!(IpCidr::parse("not-an-ip").is_err());
        assert!(IpCidr::parse("10.0.0.0/nine").is_err());
        assert!(IpCidr::parse("10.0.0.0/33").is_err());
        assert!(IpCidr::parse("::1/129").is_err());
    }

    #[test]
    fn matches_only_inside_the_block() {
        let block = IpCidr::parse("10.0.0.0/8").unwrap();
        assert!(block.contains(&"10.1.2.3".parse().unwrap()));
        assert!(!block.contains(&"11.0.0.1".parse().unwrap()));

        let narrow = IpCidr::parse("192.168.1.128/25").unwrap();
        assert!(narrow.contains(&"192.168.1.200".parse().unwrap()));
        assert!(!narrow.contains(&"192.168.1.127".parse().unwrap()));
    }

    #[test]
    fn matches_ipv4_mapped_peers() {
        let block = IpCidr::parse("10.0.0.0/8").unwrap();
        assert!(block.contains(&"::ffff:10.1.2.3".parse().unwrap()));
    }

    #[test]
    fn an_ipv4_mapped_block_trusts_the_ipv4_peer() {
        // `::ffff:10.0.0.0/104` is `10.0.0.0/8`. The peer arrives as IPv4.
        let trusted = proxies(&["::ffff:10.0.0.0/104"]);
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9")]),
            Some("10.0.0.1"),
            &trusted,
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));

        let outside = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9")]),
            Some("11.0.0.1"),
            &trusted,
        );
        assert_eq!(outside.as_deref(), Some("11.0.0.1"));
    }

    #[test]
    fn an_ipv4_mapped_block_narrower_than_the_mapped_prefix_stays_ipv6() {
        // `/64` covers more than the mapped range, so it is a real IPv6 block.
        let block = IpCidr::parse("::ffff:0:0/64").unwrap();
        assert!(block.contains(&"::ffff:0:0:1".parse().unwrap()));
        assert!(!block.contains(&"10.0.0.1".parse().unwrap()));
    }

    #[test]
    fn does_not_match_across_families() {
        let block = IpCidr::parse("10.0.0.0/8").unwrap();
        assert!(!block.contains(&"2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn ignores_forwarding_headers_with_no_trusted_proxy() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.2.3.4")]),
            Some("203.0.113.9"),
            &TrustedProxies::default(),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn ignores_forwarding_headers_from_an_untrusted_peer() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.2.3.4")]),
            Some("203.0.113.9"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn reads_the_client_through_a_trusted_peer() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn skips_trusted_hops_on_the_right() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9, 10.0.0.7, 10.0.0.1")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn ignores_a_spoofed_prefix() {
        // The client claims 1.1.1.1. The proxy appended the real address, so the
        // rightmost untrusted entry wins.
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.1.1.1, 203.0.113.9, 10.0.0.1")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn keeps_an_internal_client_when_every_hop_is_trusted() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "10.0.0.55, 10.0.0.1")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.55"));
    }

    #[test]
    fn falls_back_to_x_real_ip_behind_a_trusted_peer() {
        let resolved = resolve(
            &headers(&[("x-real-ip", "203.0.113.9")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn falls_back_to_the_peer_when_headers_are_junk() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "not-an-ip")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn malformed_hop_does_not_expose_a_spoofed_prefix() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.1.1.1, not-an-ip, 10.0.0.1")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn accepts_ipv4_and_bracketed_ipv6_hops_with_ports() {
        let trusted = proxies(&["10.0.0.0/8"]);
        let ipv4 = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9:54321")]),
            Some("10.0.0.1"),
            &trusted,
        );
        let ipv6 = resolve(
            &headers(&[("x-forwarded-for", "[2001:db8::9]:54321")]),
            Some("10.0.0.1"),
            &trusted,
        );
        assert_eq!(ipv4.as_deref(), Some("203.0.113.9"));
        assert_eq!(ipv6.as_deref(), Some("2001:db8::9"));
    }

    #[test]
    fn rejects_a_malformed_x_real_ip() {
        let resolved = resolve(
            &headers(&[("x-real-ip", "attacker-chosen-bucket")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn normalizes_an_ipv4_mapped_peer_to_one_bucket() {
        let proxies = TrustedProxies::default();
        let plain = resolve(&headers(&[]), Some("10.0.0.1"), &proxies);
        let mapped = resolve(&headers(&[]), Some("::ffff:10.0.0.1"), &proxies);
        assert_eq!(plain, mapped);
    }

    #[test]
    fn returns_none_without_a_peer_or_usable_header() {
        assert_eq!(
            resolve(&headers(&[]), None, &TrustedProxies::default()),
            None
        );
    }
}
