"""The outbound-URL guard for the crawler.

## What this defends against

``POST /ingest/crawl`` takes a URL from a caller and makes the server fetch
it. Without a guard that is a server-side request forgery primitive: the
caller borrows the server's network position, which reaches things they
cannot reach themselves — cloud instance metadata at ``169.254.169.254``
(credentials, in one request), Postgres and MinIO on the compose network,
anything else bound to loopback, and every host on the private network the
container sits in.

Authentication reduces *who* can trigger this; it does not change what it
does. Both defences are needed, and this one is worth having first because
it needs no decision about users.

## The checks, in order

1. The scheme must be ``http`` or ``https``. This is what excludes
   ``file:///etc/passwd``, ``gopher://`` (which can be used to speak
   arbitrary protocols through an HTTP client), and ``ftp://``.
2. The hostname is resolved to *every* address it has.
3. If **any** resolved address is private, loopback, link-local, multicast,
   reserved or unspecified, the URL is rejected. Any, not all: a name that
   resolves to one public and one internal address is an attack, not a
   coincidence.
4. If ``CRAWL_DOMAIN_ALLOWLIST`` is set, the hostname must match one of its
   entries (exactly, or as a subdomain). Unset means "any public host",
   which is the right default for a crawler whose job is to read the open
   web, but a deployment that only ever crawls its own documentation should
   set it.
5. After the response arrives, the address actually connected to is checked
   again, against the same rules.
6. Redirects are followed manually, one hop at a time, with every hop going
   through all of the above. Three hops maximum.

## Why step 5 exists, and what it does not do

Between resolving a name and connecting to it, the answer can change — the
attacker controls the DNS server, returns a public address for the check and
an internal one microseconds later. That is DNS rebinding, and it defeats a
guard that validates a *hostname* and then hands that hostname to an HTTP
client.

The complete fix is to connect to the address that was validated and carry
the original hostname only in the ``Host`` header and TLS SNI. ``httpx`` has
no supported way to pin a connection to an address, and the workaround
(requesting ``https://<ip>/`` with an overridden ``Host``) breaks certificate
verification, which trades one hole for another.

So this module does the next best thing and is explicit about the gap: it
reads the peer address off the completed connection and rejects the response
before a single byte of it is used. An attacker can still cause one blind
request to an internal address; they cannot see the answer. Combined with
``member`` authentication on the endpoint, that is a defensible position.
The residual risk is documented rather than papered over — a guard whose
limits are written down is one the next person can reason about.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Final, Iterable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

ALLOWLIST_ENV: Final[str] = "CRAWL_DOMAIN_ALLOWLIST"

# Networks exempt from the address rules, as comma-separated CIDRs.
#
# This exists for one legitimate case: a deployment that genuinely needs to
# crawl an internal site -- an intranet wiki on 10.x, say -- and for the
# crawler's own integration tests, which serve a fixture site on 127.0.0.1.
#
# It is a list of networks rather than a boolean on purpose. `ALLOW_PRIVATE=1`
# would re-open every hole this module exists to close, including the cloud
# metadata endpoint; naming `10.1.0.0/16` opens exactly that and nothing
# else. Whoever sets this should know that adding 169.254.0.0/16 hands an
# authenticated caller the instance's credentials.
EXEMPT_ENV: Final[str] = "CRAWL_ALLOW_ADDRESSES"

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

MAX_REDIRECTS: Final[int] = 3

DEFAULT_TIMEOUT: Final[float] = 30.0


class UnsafeURLError(ValueError):
    """The URL must not be fetched. The API turns this into a 400."""


@dataclass(frozen=True)
class SafeTarget:
    """A URL that passed every check, with what it resolved to."""

    url: str
    hostname: str
    addresses: tuple[str, ...]


def _unwrap(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Return the IPv4 address behind an IPv4-mapped IPv6 address.

    ``::ffff:127.0.0.1`` is loopback, but ``IPv6Address.is_loopback`` says
    False for it — the flags are defined on the v6 address itself, not on
    what it maps to. Unwrapping first is what stops that from being a bypass.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _exempt_networks() -> tuple:
    """Parse ``CRAWL_ALLOW_ADDRESSES``. An unparseable entry is ignored.

    Ignored rather than fatal: a typo in this variable must not take the
    process down, and the consequence of dropping an entry is that a URL is
    refused, which is the safe direction to fail in.
    """
    raw = os.environ.get(EXEMPT_ENV, "")
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring unparseable %s entry %r.", EXEMPT_ENV, entry)
    return tuple(networks)


def classify_address(raw: str) -> str | None:
    """Return why an address is unsafe, or ``None`` if it is fine.

    Split out from the resolution path so the whole policy can be tested
    against a table of literals without touching DNS.
    """
    try:
        address = _unwrap(ipaddress.ip_address(raw))
    except ValueError:
        return f"{raw!r} is not an IP address"

    for network in _exempt_networks():
        if address.version == network.version and address in network:
            logger.debug("%s is exempt via %s=%s.", address, EXEMPT_ENV, network)
            return None

    for attribute, reason in (
        ("is_loopback", "loopback"),
        ("is_link_local", "link-local"),
        # Before is_private: 0.0.0.0 satisfies both, and "unspecified" is the
        # more useful half of that answer to put in an error message.
        ("is_unspecified", "unspecified"),
        ("is_multicast", "multicast"),
        ("is_reserved", "reserved"),
        ("is_private", "private"),
    ):
        if getattr(address, attribute, False):
            return f"{address} is {reason}"

    # The backstop, and the check that actually decides the policy: a crawler
    # fetches the public web, so anything not globally routable is refused
    # whatever the named flags say about it.
    #
    # This is not belt-and-braces. On Python 3.12, 100.64.0.0/10 -- the
    # carrier-grade NAT range, which reaches ISP infrastructure -- reports
    # is_private=False *and* is_global=False, so the loop above lets it
    # through. The named flags have drifted between Python versions before
    # and will again; "must be globally routable" is the property that
    # actually matters, and it does not need maintaining as new special-use
    # ranges are assigned.
    if not address.is_global:
        return f"{address} is not globally routable"
    return None


def _allowlist() -> tuple[str, ...]:
    raw = os.environ.get(ALLOWLIST_ENV, "")
    return tuple(
        entry.strip().lower().lstrip(".")
        for entry in raw.split(",")
        if entry.strip()
    )


def _host_is_allowed(hostname: str, allowlist: Iterable[str]) -> bool:
    host = hostname.lower().rstrip(".")
    return any(
        host == entry or host.endswith(f".{entry}") for entry in allowlist
    )


async def _resolve(hostname: str) -> tuple[str, ...]:
    """Every address ``hostname`` resolves to.

    Uses the loop's resolver so a slow or hostile DNS server cannot block the
    event loop, which would turn this guard into its own denial of service.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            hostname, None, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Cannot resolve {hostname!r}: {exc}") from exc
    return tuple(dict.fromkeys(info[4][0] for info in infos))


async def assert_url_is_safe(url: str) -> SafeTarget:
    """Check one URL. Raises ``UnsafeURLError`` if it must not be fetched."""
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"Scheme {parts.scheme!r} is not allowed; only "
            f"{', '.join(sorted(ALLOWED_SCHEMES))} are fetched."
        )

    hostname = parts.hostname
    if not hostname:
        raise UnsafeURLError(f"URL has no host: {url!r}")

    allowlist = _allowlist()
    if allowlist and not _host_is_allowed(hostname, allowlist):
        raise UnsafeURLError(
            f"Host {hostname!r} is not in {ALLOWLIST_ENV}."
        )

    addresses = await _resolve(hostname)
    if not addresses:
        raise UnsafeURLError(f"{hostname!r} resolved to no addresses.")

    for address in addresses:
        reason = classify_address(address)
        if reason is not None:
            raise UnsafeURLError(
                f"Refusing to fetch {url!r}: it resolves to {reason}."
            )

    return SafeTarget(url=url, hostname=hostname, addresses=addresses)


def assert_peer_is_safe(response: httpx.Response, url: str) -> None:
    """Re-check the address actually connected to (see the module docstring).

    Best effort by design: not every transport exposes the underlying socket,
    and a missing peer address is not evidence of an attack. When it cannot
    be read the pre-flight check stands alone, and that is logged at debug
    rather than failing a legitimate crawl.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        logger.debug("No network stream for %s; peer address unchecked.", url)
        return
    peer = stream.get_extra_info("server_addr")
    if not peer:
        logger.debug("No peer address for %s; unchecked.", url)
        return

    reason = classify_address(peer[0])
    if reason is not None:
        raise UnsafeURLError(
            f"Refusing the response from {url!r}: the connection went to "
            f"{reason}. The hostname resolved to a public address when it was "
            f"checked, so this is a DNS-rebinding attempt."
        )


async def safe_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    """Fetch ``url``, validating it and every redirect hop.

    Redirects are followed by hand rather than by ``follow_redirects=True``,
    because the client would follow them without asking anyone — and a
    redirect to ``http://169.254.169.254/`` is the ordinary way an attacker
    gets past a guard that only inspects the URL it was handed.
    """
    owns_client = client is None
    active = client or httpx.AsyncClient(follow_redirects=False, timeout=timeout)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await assert_url_is_safe(current)
            response = await active.get(
                current, headers=headers, follow_redirects=False
            )
            assert_peer_is_safe(response, current)

            if not response.is_redirect:
                return response

            location = response.headers.get("location")
            if not location:
                raise UnsafeURLError(
                    f"{current!r} returned {response.status_code} with no "
                    f"Location header."
                )
            current = str(response.url.join(location))
        raise UnsafeURLError(
            f"More than {MAX_REDIRECTS} redirects starting at {url!r}."
        )
    finally:
        if owns_client:
            await active.aclose()
