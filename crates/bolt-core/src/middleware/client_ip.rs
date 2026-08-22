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

use ahash::AHashMap;
use std::net::IpAddr;

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

        let network = normalize(
            address
                .parse::<IpAddr>()
                .map_err(|_| format!("{entry:?} is not an IP address or CIDR block"))?,
        );
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

        Ok(IpCidr {
            network,
            prefix_len,
        })
    }

    /// Report whether `candidate` is inside this block.
    ///
    /// An IPv4 block never matches an IPv6 address, and the reverse. Both sides
    /// are normalized first, so `::ffff:10.0.0.1` matches `10.0.0.0/8`.
    pub fn contains(&self, candidate: &IpAddr) -> bool {
        match (self.network, normalize(*candidate)) {
            (IpAddr::V4(network), IpAddr::V4(candidate)) => {
                prefix_eq(&network.octets(), &candidate.octets(), self.prefix_len)
            }
            (IpAddr::V6(network), IpAddr::V6(candidate)) => {
                prefix_eq(&network.octets(), &candidate.octets(), self.prefix_len)
            }
            _ => false,
        }
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

fn is_trusted(address: &IpAddr, trusted_proxies: &[IpCidr]) -> bool {
    trusted_proxies.iter().any(|block| block.contains(address))
}

/// Resolve the address that a `key="ip"` rate limit applies to.
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
/// 5. Fall back to `X-Real-IP`, then `Remote-Addr`, then the peer address.
///
/// Returns `None` only when there is no peer address and no usable header. The
/// caller then keys on a constant.
pub fn resolve(
    headers: &AHashMap<String, String>,
    peer_addr: Option<&str>,
    trusted_proxies: &[IpCidr],
) -> Option<String> {
    let peer = peer_addr.and_then(parse_ip);
    let peer_key = || {
        peer.map(|ip| ip.to_string())
            .or_else(|| peer_addr.map(str::to_string))
    };

    if trusted_proxies.is_empty() {
        return peer_key();
    }
    match peer {
        Some(address) if is_trusted(&address, trusted_proxies) => {}
        _ => return peer_key(),
    }

    if let Some(forwarded) = headers.get("x-forwarded-for") {
        if let Some(client) = client_from_forwarded_for(forwarded, trusted_proxies) {
            return Some(client);
        }
    }
    for header in ["x-real-ip", "remote-addr"] {
        if let Some(address) = headers.get(header).and_then(|value| parse_ip(value)) {
            return Some(address.to_string());
        }
    }

    peer_key()
}

/// Find the client inside one `X-Forwarded-For` value.
///
/// The rightmost entry that is not a trusted proxy is the client. Entries that
/// do not parse are skipped: a bucket key must be an address, and a trusted
/// proxy always appends the real peer to the right of any junk.
fn client_from_forwarded_for(value: &str, trusted_proxies: &[IpCidr]) -> Option<String> {
    let mut leftmost: Option<IpAddr> = None;
    let mut rightmost_untrusted: Option<IpAddr> = None;

    for entry in value.split(',') {
        let Some(address) = parse_ip(entry) else {
            continue;
        };
        if leftmost.is_none() {
            leftmost = Some(address);
        }
        if !is_trusted(&address, trusted_proxies) {
            rightmost_untrusted = Some(address);
        }
    }

    rightmost_untrusted.or(leftmost).map(|ip| ip.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cidrs(entries: &[&str]) -> Vec<IpCidr> {
        entries.iter().map(|e| IpCidr::parse(e).unwrap()).collect()
    }

    fn headers(pairs: &[(&str, &str)]) -> AHashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
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
    fn does_not_match_across_families() {
        let block = IpCidr::parse("10.0.0.0/8").unwrap();
        assert!(!block.contains(&"2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn ignores_forwarding_headers_with_no_trusted_proxy() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.2.3.4")]),
            Some("203.0.113.9"),
            &[],
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn ignores_forwarding_headers_from_an_untrusted_peer() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "1.2.3.4")]),
            Some("203.0.113.9"),
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn reads_the_client_through_a_trusted_peer() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9")]),
            Some("10.0.0.1"),
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn skips_trusted_hops_on_the_right() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "203.0.113.9, 10.0.0.7, 10.0.0.1")]),
            Some("10.0.0.1"),
            &cidrs(&["10.0.0.0/8"]),
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
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn keeps_an_internal_client_when_every_hop_is_trusted() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "10.0.0.55, 10.0.0.1")]),
            Some("10.0.0.1"),
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.55"));
    }

    #[test]
    fn falls_back_to_x_real_ip_behind_a_trusted_peer() {
        let resolved = resolve(
            &headers(&[("x-real-ip", "203.0.113.9")]),
            Some("10.0.0.1"),
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("203.0.113.9"));
    }

    #[test]
    fn falls_back_to_the_peer_when_headers_are_junk() {
        let resolved = resolve(
            &headers(&[("x-forwarded-for", "not-an-ip")]),
            Some("10.0.0.1"),
            &cidrs(&["10.0.0.0/8"]),
        );
        assert_eq!(resolved.as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn normalizes_an_ipv4_mapped_peer_to_one_bucket() {
        let plain = resolve(&headers(&[]), Some("10.0.0.1"), &[]);
        let mapped = resolve(&headers(&[]), Some("::ffff:10.0.0.1"), &[]);
        assert_eq!(plain, mapped);
    }

    #[test]
    fn returns_none_without_a_peer_or_usable_header() {
        assert_eq!(resolve(&headers(&[]), None, &[]), None);
    }
}
