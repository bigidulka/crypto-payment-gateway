"""Bounded provider HTTP calls; mutations with uncertain outcomes are never retried."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from src.ledger.amounts import exact_decimal_sum
from src.payments.rails.base import RailError


def money_sum(rail: str, *values: Decimal) -> Decimal:
    try:
        return exact_decimal_sum(*values)
    except ValueError as exc:
        raise RailError(rail, "provider monetary facts exceed exact precision bounds") from exc


def amount(value: object, rail: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, (bool, float)):
        raise RailError(rail, "amount must be an exact decimal string", permanent=True)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RailError(rail, "invalid amount", permanent=True) from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise RailError(rail, "invalid non-finite or negative amount", permanent=True)
    return result


def identifier(value: object, rail: str, field: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise RailError(rail, f"missing or malformed {field}", permanent=True)
    result = str(value)
    if not result or len(result) > 256 or result.strip() != result:
        raise RailError(rail, f"missing or malformed {field}", permanent=True)
    return result


def payment_url(value: object, rail: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RailError(rail, "missing or malformed payment URL", permanent=True)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RailError(rail, "invalid payment URL", permanent=True)
    return value


class ProviderHttp:
    def __init__(self, rail: str, base_url: str, headers: dict[str, str], *, timeout: int = 30):
        self.rail = rail
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def request(
        self, method: str, path: str, data: dict | None = None, *, mutation: bool = False
    ) -> Any:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                json=data if method == "POST" else None,
                params=data if method == "GET" else None,
                allow_redirects=False,
            ) as response:
                try:
                    body = await response.json(
                        content_type=None,
                        loads=lambda value: json.loads(value, parse_float=Decimal),
                    )
                except (ValueError, aiohttp.ClientError) as exc:
                    raise RailError(
                        self.rail, "provider returned malformed JSON", outcome_unknown=mutation
                    ) from exc
                if not 200 <= response.status < 300:
                    # A structured validation rejection proves a failed mutation.
                    # Unstructured errors, duplicate operations, 408/5xx remain unknown.
                    problem = body.get("type", "") if isinstance(body, dict) else ""
                    definite = (
                        response.status in {400, 401, 403, 404, 422, 429}
                        and bool(problem)
                        and "duplicate" not in problem
                        and "already" not in problem
                    )
                    retry = response.headers.get("Retry-After", "")
                    raise RailError(
                        self.rail,
                        f"provider HTTP {response.status}",
                        permanent=response.status in {400, 401, 403, 404, 422},
                        outcome_unknown=mutation and not definite,
                        retry_after_seconds=int(retry)
                        if retry.isascii() and retry.isdecimal()
                        else None,
                    )
                if not isinstance(body, dict):
                    raise RailError(
                        self.rail, "provider returned malformed envelope", outcome_unknown=mutation
                    )
                return body
        except RailError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Do not include URL, headers, response text or credentials in the error.
            raise RailError(
                self.rail, "provider transport failure", outcome_unknown=mutation
            ) from exc

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
