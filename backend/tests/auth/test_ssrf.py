"""The outbound-URL guard.

`POST /ingest/crawl` makes the server fetch a URL a caller chose. Without
this guard that is a server-side request forgery primitive: the caller
borrows the server's network position and reaches cloud instance metadata,
Postgres and MinIO on the compose network, and every host on the private
network the container sits in.

The tests are mostly a table of hostile inputs, because that is what the
guard is: a list of things not to do, and the value is in the list being
complete rather than in any one entry being clever.
"""
from __future__ import annotations

import ipaddress

import httpx
import pytest

from auth import ssrf

# Every one of these was reachable before the guard existed.
UNSAFE_ADDRESSES = [
    ("169.254.169.254", "link-local"),   # cloud instance metadata
    ("169.254.170.2", "link-local"),     # ECS task metadata
    ("127.0.0.1", "loopback"),
    ("127.1.2.3", "loopback"),           # the whole 127/8, not just .0.1
    ("10.0.0.1", "private"),
    ("172.16.0.1", "private"),
    ("192.168.1.1", "private"),
    # Carrier-grade NAT. On Python 3.12 this is neither is_private nor
    # is_global, so it is caught by the routability backstop rather than by
    # a named flag -- which is the whole reason that backstop exists.
    ("100.64.0.1", "not globally routable"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "reserved"),
    ("::1", "loopback"),
    ("fe80::1", "link-local"),
    ("fc00::1", "private"),
    # IPv4-mapped IPv6: the v6 flags say nothing about the v4 address behind
    # them, so without unwrapping this is a straightforward bypass.
    ("::ffff:127.0.0.1", "loopback"),
    ("::ffff:169.254.169.254", "link-local"),
]

SAFE_ADDRESSES = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize("address,expected", UNSAFE_ADDRESSES)
def test_unsafe_addresses_are_classified(address, expected):
    reason = ssrf.classify_address(address)
    assert reason is not None, f"{address} was not rejected"
    assert expected in reason


@pytest.mark.parametrize("address", SAFE_ADDRESSES)
def test_public_addresses_pass(address):
    assert ssrf.classify_address(address) is None


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin DNS resolution, so the tests assert policy rather than the network."""

    def _install(*addresses: str):
        async def _fake(hostname: str):
            # An address literal resolves to itself. Without this the stub
            # would launder `http://169.254.169.254/` into a public address
            # and the redirect tests below would assert nothing.
            try:
                ipaddress.ip_address(hostname.strip("[]"))
            except ValueError:
                return tuple(addresses)
            return (hostname.strip("[]"),)

        monkeypatch.setattr(ssrf, "_resolve", _fake)

    return _install


class TestSchemes:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/_x",   # can be used to speak other protocols
            "ftp://example.com/x",
            "data:text/plain,hello",
            "redis://localhost:6379",
        ],
    )
    async def test_only_http_and_https_are_fetched(self, url):
        with pytest.raises(ssrf.UnsafeURLError, match="[Ss]cheme"):
            await ssrf.assert_url_is_safe(url)

    async def test_https_is_allowed(self, resolves_to):
        resolves_to("93.184.216.34")
        assert (await ssrf.assert_url_is_safe("https://example.com/")).hostname == (
            "example.com"
        )


class TestResolution:
    async def test_a_name_resolving_internally_is_refused(self, resolves_to):
        """The attack that a scheme check and a string blocklist both miss:
        an ordinary-looking public hostname whose A record points inside."""
        resolves_to("10.0.0.5")
        with pytest.raises(ssrf.UnsafeURLError, match="private"):
            await ssrf.assert_url_is_safe("https://totally-legit.example/")

    async def test_any_internal_address_is_enough(self, resolves_to):
        """Any, not all. A name answering with one public and one internal
        address is an attack, not a coincidence -- and the client would be
        free to connect to either."""
        resolves_to("93.184.216.34", "169.254.169.254")
        with pytest.raises(ssrf.UnsafeURLError, match="link-local"):
            await ssrf.assert_url_is_safe("https://mixed.example/")

    async def test_localhost_is_refused(self):
        with pytest.raises(ssrf.UnsafeURLError):
            await ssrf.assert_url_is_safe("http://localhost:8000/")

    async def test_a_url_without_a_host_is_refused(self):
        with pytest.raises(ssrf.UnsafeURLError, match="no host"):
            await ssrf.assert_url_is_safe("http:///nowhere")


class TestAllowlist:
    async def test_unset_allows_any_public_host(self, resolves_to, monkeypatch):
        monkeypatch.delenv(ssrf.ALLOWLIST_ENV, raising=False)
        resolves_to("93.184.216.34")
        await ssrf.assert_url_is_safe("https://anywhere.example/")

    async def test_a_host_outside_the_allowlist_is_refused(
        self, resolves_to, monkeypatch
    ):
        monkeypatch.setenv(ssrf.ALLOWLIST_ENV, "docs.example.com")
        resolves_to("93.184.216.34")
        with pytest.raises(ssrf.UnsafeURLError, match="ALLOWLIST"):
            await ssrf.assert_url_is_safe("https://evil.example/")

    async def test_subdomains_of_an_allowlisted_host_are_allowed(
        self, resolves_to, monkeypatch
    ):
        monkeypatch.setenv(ssrf.ALLOWLIST_ENV, "example.com")
        resolves_to("93.184.216.34")
        await ssrf.assert_url_is_safe("https://docs.example.com/guide")

    async def test_a_suffix_match_is_not_enough(self, resolves_to, monkeypatch):
        """`notexample.com` ends with `example.com` as a string but is a
        different domain. Matching on the string alone is the bug this
        asserts against."""
        monkeypatch.setenv(ssrf.ALLOWLIST_ENV, "example.com")
        resolves_to("93.184.216.34")
        with pytest.raises(ssrf.UnsafeURLError):
            await ssrf.assert_url_is_safe("https://notexample.com/")


class TestRedirects:
    """A redirect is the ordinary way past a guard that checks only the URL
    it was handed. The client must not be allowed to follow them by itself."""

    def _client(self, handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    async def test_a_redirect_to_an_internal_address_is_refused(self, resolves_to):
        resolves_to("93.184.216.34")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example":
                return httpx.Response(
                    302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
                )
            return httpx.Response(200, text="secrets")

        async with self._client(handler) as client:
            with pytest.raises(ssrf.UnsafeURLError, match="link-local"):
                await ssrf.safe_get("https://public.example/", client=client)

    async def test_a_redirect_to_another_public_host_is_followed(self, resolves_to):
        resolves_to("93.184.216.34")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "first.example":
                return httpx.Response(302, headers={"location": "https://second.example/"})
            return httpx.Response(200, text="the page")

        async with self._client(handler) as client:
            response = await ssrf.safe_get("https://first.example/", client=client)
        assert response.text == "the page"

    async def test_a_redirect_loop_terminates(self, resolves_to):
        resolves_to("93.184.216.34")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://loop.example/"})

        async with self._client(handler) as client:
            with pytest.raises(ssrf.UnsafeURLError, match="redirects"):
                await ssrf.safe_get("https://loop.example/", client=client)

    async def test_a_redirect_without_a_location_is_an_error(self, resolves_to):
        resolves_to("93.184.216.34")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        async with self._client(handler) as client:
            with pytest.raises(ssrf.UnsafeURLError, match="Location"):
                await ssrf.safe_get("https://headerless.example/", client=client)


class TestExemptions:
    """`CRAWL_ALLOW_ADDRESSES` exists for a deployment that genuinely crawls
    an intranet, and for the crawler's own fixture site on loopback. It is a
    list of networks rather than a boolean precisely so that opening one does
    not open all of them."""

    async def test_an_exempt_network_is_allowed(self, monkeypatch):
        monkeypatch.setenv(ssrf.EXEMPT_ENV, "127.0.0.0/8")
        await ssrf.assert_url_is_safe("http://127.0.0.1:8080/docs")

    async def test_addresses_outside_the_exemption_stay_blocked(self, monkeypatch):
        """The whole point of naming networks: exempting the loopback range
        must not also exempt the cloud metadata endpoint."""
        monkeypatch.setenv(ssrf.EXEMPT_ENV, "127.0.0.0/8")
        with pytest.raises(ssrf.UnsafeURLError, match="link-local"):
            await ssrf.assert_url_is_safe("http://169.254.169.254/")

    def test_an_unparseable_entry_is_ignored_not_fatal(self, monkeypatch):
        """A typo must not take the process down, and must not accidentally
        widen the exemption -- dropping the entry fails towards refusing."""
        monkeypatch.setenv(ssrf.EXEMPT_ENV, "not-a-cidr, 10.0.0.0/8")
        assert ssrf.classify_address("10.1.2.3") is None
        assert ssrf.classify_address("127.0.0.1") is not None

    def test_unset_exempts_nothing(self, monkeypatch):
        monkeypatch.delenv(ssrf.EXEMPT_ENV, raising=False)
        assert ssrf.classify_address("127.0.0.1") is not None
