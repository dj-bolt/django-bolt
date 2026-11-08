//! Client address and scheme resolution behind proxies.
//!
//! A client sends `X-Forwarded-For` and `X-Forwarded-Proto` itself. Each proxy
//! appends to `X-Forwarded-For`. The leftmost entry is therefore the value the
//! client chose, not a fact about the connection. Bolt trusts a forwarding
//! header only when a declared proxy sent it.
//!
//! `settings.BOLT_TRUSTED_PROXIES` declares those proxies as a CIDR list. The
//! list is empty by default. With an empty list Bolt ignores every forwarding
//! header and keys on the peer address.

use actix_web::http::header::{HeaderMap, HeaderName, X_FORWARDED_FOR, X_FORWARDED_PROTO};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::net::{IpAddr, SocketAddr};

/// A header name parsed once. A lookup by `&str` parses and copies the name.
const X_REAL_IP: HeaderName = HeaderName::from_static("x-real-ip");

/// One entry of the trusted proxy list.
///
/// A bare address such as `127.0.0.1` gets the full prefix length, so it
/// matches only itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IpCidr {
    block: Block,
}

/// The network bits and the prefix mask of a block. `IpCidr::new` computes
/// both once, so a check is one AND and one compare.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Block {
    V4 { network: u32, mask: u32 },
    V6 { network: u128, mask: u128 },
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
        if let IpAddr::V6(v6) = network
            && prefix_len >= 96
            && let Some(v4) = v6.to_ipv4_mapped()
        {
            return Ok(IpCidr::new(IpAddr::V4(v4), prefix_len - 96));
        }

        Ok(IpCidr::new(network, prefix_len))
    }

    /// Build the block of the first `prefix_len` bits of `network`.
    ///
    /// The mask is the complement of the host bits, `MAX >> prefix_len`. A
    /// full prefix has no host bits. `parse` rejects a longer prefix. If one
    /// gets here, it keeps every bit, so the block matches only `network`.
    fn new(network: IpAddr, prefix_len: u8) -> Self {
        let block = match network {
            IpAddr::V4(network) => {
                let mask = !(u32::MAX.checked_shr(u32::from(prefix_len)).unwrap_or(0));
                Block::V4 {
                    network: u32::from(network) & mask,
                    mask,
                }
            }
            IpAddr::V6(network) => {
                let mask = !(u128::MAX.checked_shr(u32::from(prefix_len)).unwrap_or(0));
                Block::V6 {
                    network: u128::from(network) & mask,
                    mask,
                }
            }
        };
        IpCidr { block }
    }

    /// Report whether `candidate` is inside this block.
    ///
    /// An IPv4 block matches an IPv4-mapped IPv6 peer. An IPv6 block that is
    /// wider than the mapped range stays IPv6 and never matches IPv4.
    pub fn contains(&self, candidate: &IpAddr) -> bool {
        match self.block {
            Block::V4 { network, mask } => match normalize(*candidate) {
                IpAddr::V4(candidate) => u32::from(candidate) & mask == network,
                IpAddr::V6(_) => false,
            },
            Block::V6 { network, mask } => match candidate {
                IpAddr::V6(candidate) => u128::from(*candidate) & mask == network,
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

/// The comma-separated entries of a header value, from the right, as
/// `value.rsplit(',')` gives them. A comma is one byte in UTF-8, so a byte
/// search finds it. The `char` search of `rsplit` costs more on a short value.
fn rsplit_commas(value: &str) -> impl Iterator<Item = &str> {
    let mut rest = Some(value);
    std::iter::from_fn(move || {
        let chain = rest?;
        match chain.bytes().rposition(|byte| byte == b',') {
            Some(comma) => {
                rest = Some(&chain[..comma]);
                Some(&chain[comma + 1..])
            }
            None => {
                rest = None;
                Some(chain)
            }
        }
    })
}

/// Parse a forwarding hop and normalize it. Returns `None` for anything else.
/// Plain IPs are standard; `SocketAddr` additionally accepts proxy output such
/// as `192.0.2.1:1234` and `[2001:db8::1]:443`. HTTP puts only spaces and tabs
/// around a list entry, so the trim is ASCII.
fn parse_forwarded_ip(value: &str) -> Option<IpAddr> {
    let value = value.trim_ascii();
    let address = match value.parse::<IpAddr>() {
        Ok(address) => address,
        Err(_) => value.parse::<SocketAddr>().ok()?.ip(),
    };
    Some(normalize(address))
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
/// 5. Fall back to the last `X-Real-IP` line when it is a valid address, then
///    the peer address. The proxy closest to Bolt adds the last line.
/// 6. A malformed forwarding chain falls back to the peer; it never exposes an
///    attacker-controlled entry farther left.
///
/// Returns `None` only when there is no peer address and no usable header. The
/// caller then keys on a constant.
///
/// `forwarded_for` returns each `X-Forwarded-For` header value in wire order.
/// `real_ip` returns the last `X-Real-IP` line. A `None` item or line is a
/// value that is not valid text. `resolve` calls them only for a trusted peer,
/// so a request that reaches rule 1 or 2 does no header lookup. It reads the
/// values from the right.
pub fn resolve<'a, I>(
    forwarded_for: impl FnOnce() -> I,
    real_ip: impl FnOnce() -> Option<Option<&'a str>>,
    peer_addr: Option<IpAddr>,
    trusted_proxies: &TrustedProxies,
) -> Option<IpAddr>
where
    I: DoubleEndedIterator<Item = Option<&'a str>>,
{
    let peer = peer_addr.map(normalize);

    if trusted_proxies.is_empty() {
        return peer;
    }
    match peer {
        Some(address) if trusted_proxies.contains(&address) => {}
        _ => return peer,
    }

    match client_from_forwarded_for(forwarded_for(), trusted_proxies) {
        Ok(Some(client)) => return Some(client),
        Ok(None) => {}
        // A malformed chain proves nothing. Fall back to the socket peer
        // instead of searching farther left through client-controlled data.
        Err(()) => return peer,
    }
    if let Some(value) = real_ip() {
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
        || {
            headers
                .get_all(&X_FORWARDED_FOR)
                .map(|value| value.to_str().ok())
        },
        || {
            headers
                .get_all(&X_REAL_IP)
                .next_back()
                .map(|value| value.to_str().ok())
        },
        peer_addr,
        trusted_proxies,
    )
}

/// Resolve the request scheme, `"http"` or `"https"`.
///
/// The rules follow `resolve`:
///
/// 1. A peer outside `trusted_proxies` gets the scheme of the connection.
///    `X-Forwarded-Proto` is client input, so ignore it.
/// 2. A trusted peer sets the scheme with `X-Forwarded-Proto`. Bolt reads the
///    rightmost value, because the proxy closest to Bolt adds that value.
/// 3. A value that is not `http` or `https` gives the connection scheme.
///
/// `secure` is true for a TLS connection. A mounted Django app reads the
/// result as the ASGI `scheme`, as from a WSGI or ASGI server. Django's
/// `SECURE_PROXY_SSL_HEADER` still applies on top of it.
#[inline]
pub fn resolve_scheme(
    headers: &HeaderMap,
    peer_addr: Option<IpAddr>,
    secure: bool,
    trusted_proxies: &TrustedProxies,
) -> &'static str {
    resolve_scheme_with(
        || {
            headers
                .get_all(&X_FORWARDED_PROTO)
                .next_back()
                .and_then(|value| value.to_str().ok())
        },
        peer_addr,
        secure,
        trusted_proxies,
    )
}

/// `resolve_scheme` for headers in another form, such as the test client map.
///
/// `forwarded_proto` returns the last `X-Forwarded-Proto` line. It runs only
/// for a trusted peer.
#[inline]
pub fn resolve_scheme_with<'a>(
    forwarded_proto: impl FnOnce() -> Option<&'a str>,
    peer_addr: Option<IpAddr>,
    secure: bool,
    trusted_proxies: &TrustedProxies,
) -> &'static str {
    let connection = if secure { "https" } else { "http" };
    if trusted_proxies.is_empty() {
        return connection;
    }
    match peer_addr {
        Some(address) if trusted_proxies.contains(&normalize(address)) => {}
        _ => return connection,
    }

    let forwarded = forwarded_proto()
        .and_then(|value| rsplit_commas(value).next())
        .map(str::trim_ascii);
    match forwarded {
        Some(proto) if proto.eq_ignore_ascii_case("https") => "https",
        Some(proto) if proto.eq_ignore_ascii_case("http") => "http",
        _ => connection,
    }
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
        for entry in rsplit_commas(value) {
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
    use proptest::prelude::*;
    use std::net::{Ipv4Addr, Ipv6Addr};

    proptest! {
        /// A block holds an address exactly when the XOR of the two has no
        /// set bit in the first `prefix_len` bits.
        #[test]
        fn a_block_compares_the_leading_bits(network in any::<u32>(), noise in any::<u32>(), shift in 0u32..=32, prefix_len in 0u8..=32) {
            // The addresses differ only after the first `shift` bits.
            let differing = noise.checked_shr(shift).unwrap_or(0);
            let candidate = Ipv4Addr::from(network ^ differing);
            let expected = prefix_len == 0 || differing >> (32 - u32::from(prefix_len)) == 0;
            let block = IpCidr::new(IpAddr::from(Ipv4Addr::from(network)), prefix_len);
            prop_assert_eq!(block.contains(&IpAddr::V4(candidate)), expected);
        }

        #[test]
        fn a_block_compares_the_leading_bits_of_ipv6(network in any::<u128>(), noise in any::<u128>(), shift in 0u32..=128, prefix_len in 0u8..=128) {
            let differing = noise.checked_shr(shift).unwrap_or(0);
            let candidate = Ipv6Addr::from(network ^ differing);
            let expected = prefix_len == 0 || differing >> (128 - u32::from(prefix_len)) == 0;
            let block = IpCidr::new(IpAddr::from(Ipv6Addr::from(network)), prefix_len);
            prop_assert_eq!(block.contains(&IpAddr::V6(candidate)), expected);
        }

        /// The byte search splits a value as `rsplit(',')` does, also next to
        /// a character of more than one byte.
        #[test]
        fn rsplit_commas_splits_as_rsplit(value in "[ ,.:0-9a-f\\[\\]é€]{0,40}") {
            let expected: Vec<&str> = value.rsplit(',').collect();
            prop_assert_eq!(rsplit_commas(&value).collect::<Vec<_>>(), expected);
        }
    }

    use actix_web::http::header::HeaderValue;

    /// Parse one address and normalize it, as a peer address arrives.
    fn parse_ip(value: &str) -> Option<IpAddr> {
        value.trim().parse::<IpAddr>().ok().map(normalize)
    }

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
    fn an_oversized_prefix_matches_only_its_address() {
        // `parse` rejects these. A block built with one must not match all.
        let v4 = IpCidr::new("10.0.0.1".parse().unwrap(), 40);
        assert!(v4.contains(&"10.0.0.1".parse().unwrap()));
        assert!(!v4.contains(&"10.0.0.2".parse().unwrap()));
        let v6 = IpCidr::new("2001:db8::1".parse().unwrap(), 200);
        assert!(v6.contains(&"2001:db8::1".parse().unwrap()));
        assert!(!v6.contains(&"2001:db8::2".parse().unwrap()));
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
    fn reads_the_last_x_real_ip_line() {
        // A proxy that appends `X-Real-IP` keeps the line of the client first.
        let resolved = resolve(
            &headers(&[("x-real-ip", "1.1.1.1"), ("x-real-ip", "203.0.113.9")]),
            Some("10.0.0.1"),
            &proxies(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn rules_1_and_2_read_no_header() {
        let peer = parse_ip("203.0.113.9");
        let unread = || -> std::iter::Empty<Option<&str>> { panic!("read X-Forwarded-For") };
        for trusted in [TrustedProxies::default(), proxies(&["10.0.0.0/8"])] {
            let resolved = super::resolve(unread, || panic!("read X-Real-IP"), peer, &trusted);
            assert_eq!(resolved, peer);
        }
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

    fn scheme(pairs: &[(&str, &str)], peer_addr: Option<&str>, trusted: &[&str]) -> &'static str {
        resolve_scheme(
            &headers(pairs),
            peer_addr.and_then(parse_ip),
            false,
            &proxies(trusted),
        )
    }

    #[test]
    fn scheme_ignores_forwarded_proto_without_trusted_proxies() {
        let forwarded = [("x-forwarded-proto", "https")];
        assert_eq!(scheme(&forwarded, Some("203.0.113.9"), &[]), "http");
    }

    #[test]
    fn scheme_ignores_forwarded_proto_from_an_untrusted_peer() {
        let forwarded = [("x-forwarded-proto", "https")];
        assert_eq!(
            scheme(&forwarded, Some("203.0.113.9"), &["10.0.0.0/8"]),
            "http"
        );
        assert_eq!(scheme(&forwarded, None, &["10.0.0.0/8"]), "http");
    }

    #[test]
    fn scheme_follows_forwarded_proto_from_a_trusted_peer() {
        let trusted = ["10.0.0.0/8"];
        let https = [("x-forwarded-proto", "https")];
        assert_eq!(scheme(&https, Some("10.0.0.1"), &trusted), "https");
        assert_eq!(scheme(&https, Some("::ffff:10.0.0.1"), &trusted), "https");
        assert_eq!(
            scheme(
                &[("x-forwarded-proto", " HTTPS ")],
                Some("10.0.0.1"),
                &trusted
            ),
            "https"
        );
    }

    #[test]
    fn scheme_follows_forwarded_http_from_a_trusted_peer_on_tls() {
        // The proxy reached Bolt over TLS, but the client used plain HTTP.
        let trusted = proxies(&["10.0.0.0/8"]);
        for value in ["http", " HTTP "] {
            assert_eq!(
                resolve_scheme(
                    &headers(&[("x-forwarded-proto", value)]),
                    parse_ip("10.0.0.1"),
                    true,
                    &trusted,
                ),
                "http",
                "{value:?}"
            );
        }
    }

    #[test]
    fn scheme_reads_the_rightmost_forwarded_proto() {
        let trusted = ["10.0.0.0/8"];
        // A client sent `https`, and the proxy appended the real scheme.
        assert_eq!(
            scheme(
                &[("x-forwarded-proto", "https, http")],
                Some("10.0.0.1"),
                &trusted
            ),
            "http"
        );
        assert_eq!(
            scheme(
                &[
                    ("x-forwarded-proto", "https"),
                    ("x-forwarded-proto", "http")
                ],
                Some("10.0.0.1"),
                &trusted
            ),
            "http"
        );
    }

    #[test]
    fn scheme_falls_back_to_the_connection_for_an_unknown_value() {
        let trusted = ["10.0.0.0/8"];
        for value in ["", "ftp", "https;", "https http"] {
            assert_eq!(
                scheme(&[("x-forwarded-proto", value)], Some("10.0.0.1"), &trusted),
                "http",
                "{value:?}"
            );
            assert_eq!(
                resolve_scheme(
                    &headers(&[("x-forwarded-proto", value)]),
                    parse_ip("10.0.0.1"),
                    true,
                    &proxies(&trusted),
                ),
                "https",
                "{value:?}"
            );
        }
        assert_eq!(scheme(&[], Some("10.0.0.1"), &trusted), "http");
    }

    proptest! {
        /// A peer that is not a trusted proxy never changes the scheme.
        #[test]
        fn scheme_from_an_untrusted_peer_is_the_connection_scheme(value in "[ -~]{0,40}", secure in any::<bool>(), octet in 0u8..=255) {
            let headers = headers(&[("x-forwarded-proto", value.as_str())]);
            let peer = Some(IpAddr::from([203, 0, 113, octet]));
            let resolved = resolve_scheme(&headers, peer, secure, &proxies(&["10.0.0.0/8"]));
            prop_assert_eq!(resolved, if secure { "https" } else { "http" });
        }

        /// A trusted peer gets `https` only for a rightmost value of `https`.
        #[test]
        fn scheme_from_a_trusted_peer_is_https_only_for_https(value in "[ -~]{0,40}") {
            let headers = headers(&[("x-forwarded-proto", value.as_str())]);
            let peer = Some(IpAddr::from([10, 0, 0, 1]));
            let resolved = resolve_scheme(&headers, peer, false, &proxies(&["10.0.0.0/8"]));
            let rightmost = value.rsplit(',').next().unwrap_or("").trim();
            prop_assert_eq!(resolved == "https", rightmost.eq_ignore_ascii_case("https"));
        }
    }
}
