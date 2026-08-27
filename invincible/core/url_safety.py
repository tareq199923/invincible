# invincible/core/url_safety.py
"""SSRF guard for user-supplied provider base URLs (Platform Phase 9).

A BYOK user can type any base URL. Before it is stored, probed, or used
for routing, :func:`validate_public_https_url` checks that the URL is
``https://`` and that the host - as a literal or via every DNS answer -
does not point at private/loopback/link-local space (RFC1918, loopback,
the 169.254.169.254 cloud-metadata range, CGNAT, unspecified, IPv6 ULA/
link-local/multicast, and IPv4-mapped equivalents). Internal names
(``localhost``, dotless single-label hosts, ``*.localhost``) and URLs
with embedded userinfo are rejected outright.

The check runs at CREATE time and again before every TEST/chat use, so a
DNS rebinding between "add" and "use" cannot bypass it. The resolver is
injectable so tests stay hermetic (no real DNS).
"""
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

Resolver = Callable[[str], list[str]]

_BLOCKED_V4_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",        # "this host"
        "10.0.0.0/8",       # RFC1918
        "100.64.0.0/10",    # CGNAT shared space
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local incl. 169.254.169.254 metadata
        "172.16.0.0/12",    # RFC1918
        "192.168.0.0/16",   # RFC1918
        "224.0.0.0/4",      # multicast
    )
)
_BLOCKED_V6_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",       # unspecified
        "::1/128",      # loopback
        "fc00::/7",     # unique local addresses (ULA)
        "fe80::/10",    # link-local
        "ff00::/8",     # multicast
    )
)


class UnsafeUrlError(ValueError):
    """The URL is not a public https:// target (SSRF guard)."""


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        # ::ffff:a.b.c.d is an IPv4 address in disguise - judge it as one.
        ip = ip.ipv4_mapped
    networks = _BLOCKED_V4_NETWORKS if ip.version == 4 else _BLOCKED_V6_NETWORKS
    return any(ip in network for network in networks)


def _default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def validate_public_https_url(url: str, *, resolve: Resolver | None = None) -> None:
    """Raise :class:`UnsafeUrlError` unless ``url`` is an https URL whose
    host (literal or every DNS answer) is a public address. Returns None
    on success. ``resolve`` defaults to the module's resolver at CALL time
    so tests can monkeypatch ``url_safety._default_resolve``."""
    resolve = resolve or _default_resolve
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("base URL is empty")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise UnsafeUrlError("base URL must use https://")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("base URL has no host")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("base URL must not embed credentials")
    host = host.strip().rstrip(".").lower()

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked(literal):
            raise UnsafeUrlError("base URL host is a blocked address range")
        return

    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        # Dotless single labels resolve via search domains / internal DNS;
        # a public API host always carries a dot.
        raise UnsafeUrlError("base URL host is not a public name")

    try:
        answers = resolve(host)
    except OSError as exc:
        raise UnsafeUrlError("base URL host does not resolve") from exc
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer)
        except ValueError:
            continue
        if _is_blocked(ip):
            raise UnsafeUrlError(
                "base URL host resolves to a blocked address range"
            )
