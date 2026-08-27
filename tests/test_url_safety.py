# tests/test_url_safety.py
"""SSRF guard unit tests (Platform Phase 9, fully hermetic).

Every blocked range from the guard's inventory is pinned, plus the
scheme/host/userinfo rejections and a public pass-through. DNS is faked
via the injectable resolver - no real lookups ever happen.
"""
import pytest

from invincible.core.url_safety import UnsafeUrlError, validate_public_https_url


def fake_resolver(answers):
    def _resolve(host):
        return answers
    return _resolve


# --- blocked ranges (literal IPs; no DNS needed) --------------------------


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/v1",           # loopback v4
    "https://127.8.8.8/v1",           # loopback range edge
    "https://10.1.2.3/v1",            # RFC1918 10/8
    "https://172.16.0.1/v1",          # RFC1918 172.16/12 low
    "https://172.31.255.255/v1",      # RFC1918 172.16/12 high
    "https://192.168.1.50/v1",        # RFC1918 192.168/16
    "https://169.254.169.254/v1",     # cloud metadata
    "https://169.254.10.10/v1",       # link-local generally
    "https://100.64.0.1/v1",          # CGNAT shared space
    "https://0.0.0.0/v1",             # unspecified
    "https://224.0.0.1/v1",           # multicast
    "https://[::1]/v1",               # loopback v6
    "https://[fc00::1]/v1",           # ULA v6
    "https://[fd12:3456::1]/v1",      # ULA v6 (fd00::/8)
    "https://[fe80::1]/v1",           # link-local v6
    "https://[ff02::1]/v1",           # multicast v6
    "https://[::ffff:10.0.0.1]/v1",   # IPv4-mapped private
    "https://[::ffff:169.254.169.254]/v1",  # mapped metadata
])
def test_blocked_ip_literals(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_https_url(url, resolve=fake_resolver(["93.184.216.34"]))


@pytest.mark.parametrize("host,answers", [
    # DNS answers pointing at private space are rejected per-answer.
    ("rebind.example.com", ["10.0.0.7"]),
    ("rebind.example.com", ["93.184.216.34", "192.168.0.9"]),  # one bad answer
    ("rebind.example.com", ["fd00::5"]),
])
def test_blocked_dns_answers(host, answers):
    with pytest.raises(UnsafeUrlError):
        validate_public_https_url(
            f"https://{host}/v1", resolve=fake_resolver(answers))


# --- scheme / host / shape rejections -------------------------------------


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1",      # https only
    "ftp://api.example.com/v1",
    "api.example.com/v1",             # no scheme at all
    "https://localhost/v1",           # internal name
    "https://sub.localhost/v1",
    "https://myprovider/v1",          # dotless single label
    "https://user:key@api.example.com/v1",  # embedded credentials
    "https:///v1",                    # no host
    "",
])
def test_rejected_shapes(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_https_url(url, resolve=fake_resolver(["93.184.216.34"]))


def test_unresolvable_host_rejected():
    def _boom(host):
        raise OSError("no such host")
    with pytest.raises(UnsafeUrlError):
        validate_public_https_url(
            "https://nope.example.com/v1", resolve=_boom)


# --- pass-through ----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://api.example.com/v1",
    "https://api.example.com.",       # trailing dot is normalized
    "https://93.184.216.34/v1",       # public literal
    "https://[2606:4700::1111]/v1",   # public v6 literal
])
def test_public_urls_accepted(url):
    validate_public_https_url(url, resolve=fake_resolver(["93.184.216.34"]))


def test_every_dns_answer_must_be_public():
    validate_public_https_url(
        "https://api.example.com/v1",
        resolve=fake_resolver(["93.184.216.34", "2606:4700::1111"]),
    )
