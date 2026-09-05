"""Public HTTPS webhook validation and DNS-pinned aiohttp transport."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class WebhookEgressError(ValueError):
    """A webhook destination violates the permanent public-egress policy."""


class WebhookResolutionError(WebhookEgressError):
    """DNS failed transiently; callers can retry without policy-denial semantics."""


Lookup = Callable[[str, int], Awaitable[list[tuple[int, str]]]]


@dataclass(frozen=True)
class WebhookDestination:
    """A canonical URL and the only peers a delivery is allowed to connect to."""

    url: str
    host: str
    port: int
    addresses: tuple[tuple[int, str], ...]


def _normalized_host(host: str) -> str:
    """Return the canonical ASCII IDNA host used for URL, DNS, SNI and pinning."""
    try:
        canonical = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebhookEgressError("webhook URL hostname is invalid") from exc
    labels = canonical.split(".")
    if (
        not canonical
        or len(canonical) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise WebhookEgressError("webhook URL hostname is invalid")
    return canonical


def validate_webhook_url(value: str) -> SplitResult:
    """Validate and canonicalize a public HTTPS DNS webhook URL before storage."""
    if not isinstance(value, str) or not value:
        raise WebhookEgressError("webhook URL must be a string")
    if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value):
        raise WebhookEgressError("webhook URL contains invalid characters")

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https":
        raise WebhookEgressError("webhook URL must use https")
    if not parsed.hostname:
        raise WebhookEgressError("webhook URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise WebhookEgressError("webhook URL must not include userinfo")
    if parsed.fragment:
        raise WebhookEgressError("webhook URL must not include a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebhookEgressError("webhook URL has an invalid port") from exc
    if port == 0 or (port is not None and not 1 <= port <= 65535):
        raise WebhookEgressError("webhook URL has an invalid port")

    host = _normalized_host(parsed.hostname)
    if "%" in host:
        raise WebhookEgressError("webhook URL hostname is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WebhookEgressError("webhook URL must use a DNS hostname, not an IP literal")

    netloc = host if port is None else f"{host}:{port}"
    return parsed._replace(scheme="https", netloc=netloc, fragment="")


def _is_global_address(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


async def _system_lookup(host: str, port: int) -> list[tuple[int, str]]:
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, OSError) as exc:
        raise WebhookResolutionError("webhook DNS lookup failed") from exc
    return [(family, sockaddr[0]) for family, _, _, _, sockaddr in answers]


async def resolve_webhook_destination(
    url: str,
    *,
    lookup: Lookup | None = None,
    timeout_seconds: float = 10,
) -> WebhookDestination:
    """Resolve all answers within a deadline and allow only public peers."""
    parsed = validate_webhook_url(url)
    host = _normalized_host(parsed.hostname or "")
    port = parsed.port or 443
    try:
        answers = await asyncio.wait_for((lookup or _system_lookup)(host, port), timeout_seconds)
    except TimeoutError as exc:
        raise WebhookResolutionError("webhook DNS lookup timed out") from exc
    except (socket.gaierror, OSError) as exc:
        raise WebhookResolutionError("webhook DNS lookup failed") from exc
    if not answers:
        raise WebhookResolutionError("webhook hostname did not resolve")

    unique_answers = tuple(dict.fromkeys(answers))
    if not all(_is_global_address(address) for _, address in unique_answers):
        raise WebhookEgressError("webhook hostname resolves to a non-public address")
    return WebhookDestination(
        url=urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, "")),
        host=host,
        port=port,
        addresses=unique_answers,
    )


class PinnedWebhookResolver(AbstractResolver):
    """Return only DNS answers already validated for a single destination."""

    def __init__(self, destination: WebhookDestination):
        self._destination = destination

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        if _normalized_host(host) != self._destination.host or port != self._destination.port:
            raise OSError("unexpected webhook resolver target")
        return [
            {
                "hostname": self._destination.host,
                "host": address,
                "port": self._destination.port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for address_family, address in self._destination.addresses
            if family in (socket.AF_UNSPEC, address_family)
        ]

    async def close(self) -> None:
        return None


def pinned_webhook_session(
    destination: WebhookDestination,
    *,
    timeout_seconds: float,
    ssl_context: ssl.SSLContext | None = None,
) -> aiohttp.ClientSession:
    """Create one no-redirect, DNS-pinned session for one webhook delivery.

    ``ssl_context`` exists only for controlled local transport tests; production
    callers omit it and aiohttp performs normal certificate verification.
    """
    connector = aiohttp.TCPConnector(
        resolver=PinnedWebhookResolver(destination),
        use_dns_cache=False,
        ttl_dns_cache=0,
        ssl=ssl_context,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds, connect=min(timeout_seconds, 10)),
    )
