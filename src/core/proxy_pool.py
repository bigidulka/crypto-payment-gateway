"""SOCKS5/HTTP proxy rotation pool.

Loads proxies from a text file (one per line, format ``ip:port:user:pass``)
or from a single ``PROXY_DEFAULT`` env var, and rotates them round-robin
for each ``httpx.AsyncClient`` instance.

Used by OKLink scanner to avoid 429 rate-limiting from a single IP.
"""

from __future__ import annotations

import logging
import itertools
import threading
from pathlib import Path
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyEntry:
    """Parsed proxy ready for httpx ``proxy=`` parameter."""

    url: str  # e.g. socks5://user:pass@ip:port
    raw: str  # original line for logging


@dataclass
class ProxyPool:
    """Thread-safe round-robin proxy rotator."""

    _entries: list[ProxyEntry] = field(default_factory=list)
    _cycle: Iterator[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def next_proxy(self) -> str | None:
        """Return next proxy URL in round-robin order, or ``None`` if pool is empty."""
        if not self._entries:
            return None
        with self._lock:
            if self._cycle is None:
                self._cycle = itertools.cycle(e.url for e in self._entries)
            return next(self._cycle)

    def all_urls(self) -> list[str]:
        return [e.url for e in self._entries]


def _parse_line(line: str) -> ProxyEntry | None:
    """Parse a single proxy line in ``ip:port:user:pass`` format."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    # Already a URL? (socks5://... or http://...)
    if "://" in stripped:
        return ProxyEntry(url=stripped, raw=stripped)

    parts = stripped.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        url = f"socks5://{user}:{pwd}@{ip}:{port}"
        return ProxyEntry(url=url, raw=stripped)
    elif len(parts) == 2:
        ip, port = parts
        url = f"socks5://{ip}:{port}"
        return ProxyEntry(url=url, raw=stripped)

    logger.warning(f"Skipping malformed proxy line: {stripped[:40]}...")
    return None


def load_proxy_file(path: str | Path) -> ProxyPool:
    """Load proxies from a text file (one per line, ``ip:port:user:pass``)."""
    p = Path(path)
    if not p.is_file():
        logger.warning(f"Proxy file not found: {p}")
        return ProxyPool()

    entries: list[ProxyEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        entry = _parse_line(line)
        if entry is not None:
            entries.append(entry)

    pool = ProxyPool(_entries=entries)
    logger.info(f"Loaded {len(pool)} proxies from {p}")
    return pool


def load_proxy_default(url: str) -> ProxyPool:
    """Create a single-proxy pool from a ``PROXY_DEFAULT`` URL."""
    if not url.strip():
        return ProxyPool()
    entry = _parse_line(url.strip())
    if entry is None:
        return ProxyPool()
    pool = ProxyPool(_entries=[entry])
    logger.info(f"Loaded 1 proxy from PROXY_DEFAULT")
    return pool


def build_proxy_pool(
    proxy_file: str = "",
    proxy_default: str = "",
) -> ProxyPool:
    """
    Build a proxy pool from settings.

    Priority:
    1. ``proxy_file`` — load from file (many proxies, round-robin)
    2. ``proxy_default`` — single proxy URL
    3. Empty pool (no proxy)
    """
    if proxy_file:
        pool = load_proxy_file(proxy_file)
        if not pool.is_empty:
            return pool
        logger.warning(f"Proxy file {proxy_file} yielded no proxies, falling back")

    if proxy_default:
        return load_proxy_default(proxy_default)

    return ProxyPool()


# ── Module-level singleton ──────────────────────────────────────────────

_pool: ProxyPool | None = None
_pool_lock = threading.Lock()


def init_proxy_pool(proxy_file: str = "", proxy_default: str = "") -> ProxyPool:
    """Initialize and cache the global proxy pool."""
    global _pool
    with _pool_lock:
        _pool = build_proxy_pool(proxy_file, proxy_default)
        return _pool


def get_proxy_pool() -> ProxyPool:
    """Get the cached proxy pool, initializing from env if needed."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from src.core.config import get_settings
                s = get_settings()
                _pool = build_proxy_pool(
                    proxy_file=s.proxy_file,
                    proxy_default=s.proxy_default,
                )
    return _pool