"""OKLink-backed token transfer scanner.

Client uses OKLink explorer endpoints to discover incoming ERC-20/BEP-20
transfers without JSON-RPC log scans. RPC remains needed for signed
transactions and optional chain head checks.
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from secrets import randbelow
from typing import Any

import httpx


class OKLinkClientError(RuntimeError):
    """Base OKLink client error."""


class OKLinkAPIError(OKLinkClientError):
    """OKLink returned a non-zero code or malformed response."""


@dataclass(frozen=True)
class OKLinkClientConfig:
    """Runtime config for OKLink explorer client."""

    base_url: str
    api_prefix: str
    referer: str
    user_agent: str
    web_key: str
    transfer_event_signature: str
    page_limit: int
    request_timeout_seconds: float
    request_delay_seconds: float
    max_pages_per_address: int
    max_log_pages_per_tx: int
    api_key_time_shift_ms: int


class OKLinkFetchMethod(Enum):
    """Non-RPC OKLink fetch method marker."""

    ADDRESS_TOKEN_TRANSFERS = "oklink_address_token_transfers"


@dataclass(frozen=True)
class OKLinkTokenTransfer:
    """Address token-transfer row from OKLink."""

    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    token_contract: str
    value: Decimal | None


@dataclass(frozen=True)
class OKLinkIncomingScanResult:
    """Incoming transfer scan result before poller adapter conversion."""

    logs: list[dict[str, Any]]
    is_complete: bool
    failed_address_count: int


@dataclass(frozen=True)
class OKLinkTransferLogResult:
    """Transfer log fetch result compatible with persistent poller usage."""

    logs: list[dict[str, Any]]
    method_used: OKLinkFetchMethod
    rpc_used: str
    latency_ms: float
    from_block: int
    to_block: int
    is_complete: bool
    failed_address_count: int


class OKLinkExplorerClient:
    """Async OKLink explorer client for token-transfer discovery."""

    def __init__(
        self,
        config: OKLinkClientConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=config.request_timeout_seconds,
        )
        self._owns_client = http_client is None

        self._validate_config()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("base_url", self.config.base_url),
                ("api_prefix", self.config.api_prefix),
                ("referer", self.config.referer),
                ("user_agent", self.config.user_agent),
                ("web_key", self.config.web_key),
                ("transfer_event_signature", self.config.transfer_event_signature),
            )
            if not str(value).strip()
        ]
        if missing:
            raise OKLinkClientError(f"missing OKLink config fields: {', '.join(missing)}")
        if self.config.page_limit <= 0:
            raise OKLinkClientError("OKLink page_limit must be positive")
        if self.config.max_pages_per_address <= 0:
            raise OKLinkClientError("OKLink max_pages_per_address must be positive")
        if self.config.max_log_pages_per_tx <= 0:
            raise OKLinkClientError("OKLink max_log_pages_per_tx must be positive")
        if self.config.api_key_time_shift_ms <= 0:
            raise OKLinkClientError("OKLink api_key_time_shift_ms must be positive")

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "user-agent": self.config.user_agent,
            "referer": self.config.referer,
            "x-apiKey": generate_oklink_web_api_key(
                self.config.web_key,
                self.config.api_key_time_shift_ms,
            ),
        }

    async def _request(self, path: str, params: dict[str, str]) -> Any:
        query = {key: value for key, value in params.items() if value != ""}
        query["t"] = str(int(time.time() * 1000))
        normalized_path = "/" + self.config.api_prefix.strip("/") + "/" + path.lstrip("/")

        try:
            response = await self._client.get(
                normalized_path,
                params=query,
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OKLinkClientError(f"OKLink request failed: {exc}") from exc

        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise OKLinkAPIError("OKLink response is not valid JSON") from exc

        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (0, "0", None, ""):
            message = payload.get("msg") or payload.get("detailMsg") or payload.get("error_message")
            raise OKLinkAPIError(f"OKLink API error: {message or code}")

        await self._pace()
        return payload.get("data") if isinstance(payload, dict) else payload

    async def _pace(self) -> None:
        if self.config.request_delay_seconds > 0:
            await asyncio.sleep(self.config.request_delay_seconds)

    async def fetch_address_token_transfers(
        self,
        chain: str,
        address: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[OKLinkTokenTransfer]:
        """Fetch one OKLink token-transfer page for one address."""
        page_limit = limit or self.config.page_limit
        path = f"v2/{chain}/addresses/{address}/transfers/condition/token"
        data = await self._request(
            path,
            {
                "offset": str(offset),
                "limit": str(page_limit),
                "address": address,
            },
        )

        if not isinstance(data, dict):
            raise OKLinkAPIError("OKLink address token-transfer response must be object")

        hits = data.get("hits")
        if not isinstance(hits, list):
            raise OKLinkAPIError("OKLink address token-transfer response missing hits")

        return [_parse_token_transfer(item) for item in hits if isinstance(item, dict)]

    async def fetch_transaction_logs(
        self,
        chain: str,
        tx_hash: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch one OKLink log page for transaction."""
        page_limit = limit or self.config.page_limit
        path = f"v1/{chain}/transactions/{tx_hash}/logs"
        data = await self._request(
            path,
            {
                "offset": str(offset),
                "limit": str(page_limit),
            },
        )

        if not isinstance(data, list):
            raise OKLinkAPIError("OKLink transaction logs response must be list")

        return [item for item in data if isinstance(item, dict)]

    async def fetch_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
        *,
        chain: str,
    ) -> OKLinkTransferLogResult:
        """Fetch logs via OKLink with persistent-poller-compatible result shape."""
        start = time.monotonic()
        scan_result = await self._fetch_incoming_token_transfer_logs_with_status(
            chain,
            to_addresses,
            token_contracts,
            from_block,
            to_block,
        )
        return OKLinkTransferLogResult(
            logs=scan_result.logs,
            method_used=OKLinkFetchMethod.ADDRESS_TOKEN_TRANSFERS,
            rpc_used="oklink",
            latency_ms=(time.monotonic() - start) * 1000,
            from_block=from_block,
            to_block=to_block,
            is_complete=scan_result.is_complete,
            failed_address_count=scan_result.failed_address_count,
        )

    async def fetch_incoming_token_transfer_logs(
        self,
        chain: str,
        to_addresses: list[str],
        token_contracts: list[str],
        from_block: int,
        to_block: int,
    ) -> list[dict[str, Any]]:
        """Fetch incoming token transfers as Web3-compatible log dictionaries."""
        result = await self._fetch_incoming_token_transfer_logs_with_status(
            chain,
            to_addresses,
            token_contracts,
            from_block,
            to_block,
        )
        return result.logs

    async def _fetch_incoming_token_transfer_logs_with_status(
        self,
        chain: str,
        to_addresses: list[str],
        token_contracts: list[str],
        from_block: int,
        to_block: int,
    ) -> OKLinkIncomingScanResult:
        """Fetch incoming token transfers with completeness metadata."""
        watched_addresses = {_normalize_address(address) for address in to_addresses}
        watched_tokens = {_normalize_address(address) for address in token_contracts}
        candidate_tx_hashes: set[str] = set()
        failed_address_count = 0

        for address in sorted(watched_addresses):
            offset = 0
            address_scan_complete = False
            for _ in range(self.config.max_pages_per_address):
                transfers = await self.fetch_address_token_transfers(
                    chain,
                    address,
                    offset=offset,
                    limit=self.config.page_limit,
                )
                if not transfers:
                    address_scan_complete = True
                    break

                reached_older_blocks = False
                for transfer in transfers:
                    if transfer.block_number < from_block:
                        reached_older_blocks = True
                        continue
                    if transfer.block_number > to_block:
                        continue
                    if _normalize_address(transfer.to_address) != address:
                        continue
                    if _normalize_address(transfer.token_contract) not in watched_tokens:
                        continue
                    candidate_tx_hashes.add(transfer.tx_hash)

                if reached_older_blocks or len(transfers) < self.config.page_limit:
                    address_scan_complete = True
                    break
                offset += self.config.page_limit

            if not address_scan_complete:
                failed_address_count += 1

        logs: list[dict[str, Any]] = []
        seen_logs: set[tuple[str, int]] = set()
        for tx_hash in sorted(candidate_tx_hashes):
            tx_logs, tx_logs_complete = await self._fetch_all_transaction_logs(chain, tx_hash)
            if not tx_logs_complete:
                failed_address_count = max(failed_address_count, 1)
            for log in tx_logs:
                converted = _convert_oklink_log(log, self.config.transfer_event_signature)
                if converted is None:
                    continue
                block_number = _int_value(converted.get("blockNumber"), 0)
                if block_number < from_block or block_number > to_block:
                    continue
                topics = converted.get("topics", [])
                if len(topics) < 3:
                    continue
                to_address = _topic_to_address(topics[2])
                token_contract = _normalize_address(str(converted.get("address", "")))
                if to_address not in watched_addresses or token_contract not in watched_tokens:
                    continue
                identity = (
                    str(converted["transactionHash"]),
                    _int_value(converted.get("logIndex"), 0),
                )
                if identity in seen_logs:
                    continue
                seen_logs.add(identity)
                logs.append(converted)

        logs.sort(
            key=lambda item: (
                _int_value(item.get("blockNumber"), 0),
                _int_value(item.get("logIndex"), 0),
            )
        )
        return OKLinkIncomingScanResult(
            logs=logs,
            is_complete=failed_address_count == 0,
            failed_address_count=failed_address_count,
        )

    async def _fetch_all_transaction_logs(
        self,
        chain: str,
        tx_hash: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        logs: list[dict[str, Any]] = []
        offset = 0
        logs_complete = False
        for _ in range(self.config.max_log_pages_per_tx):
            page = await self.fetch_transaction_logs(
                chain,
                tx_hash,
                offset=offset,
                limit=self.config.page_limit,
            )
            logs.extend(page)
            if len(page) < self.config.page_limit:
                logs_complete = True
                break
            offset += self.config.page_limit
        return logs, logs_complete


class OKLinkTransferLogFetcher:
    """Chain-bound fetcher adapter for persistent poller overrides."""

    def __init__(self, chain: str, client: OKLinkExplorerClient) -> None:
        if not chain.strip():
            raise OKLinkClientError("OKLink fetcher chain is required")
        self.chain = chain.strip()
        self.client = client

    async def fetch_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
    ) -> OKLinkTransferLogResult:
        return await self.client.fetch_transfer_logs(
            from_block,
            to_block,
            to_addresses,
            token_contracts,
            chain=self.chain,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def generate_oklink_web_api_key(
    web_key: str,
    api_key_time_shift_ms: int,
    now_ms: int | None = None,
    nonce: int | None = None,
) -> str:
    """Generate OKLink browser x-apiKey value used by explorer endpoints."""
    if not web_key:
        raise OKLinkClientError("OKLink web_key is required")
    if api_key_time_shift_ms <= 0:
        raise OKLinkClientError("OKLink api_key_time_shift_ms must be positive")
    shifted_key = web_key[8:] + web_key[:8] if len(web_key) > 8 else web_key
    timestamp_ms = int(time.time() * 1000) if now_ms is None else now_ms
    suffix = randbelow(1000) if nonce is None else nonce
    encrypted_time = f"{timestamp_ms + api_key_time_shift_ms}{suffix:03d}"
    return base64.b64encode(f"{shifted_key}|{encrypted_time}".encode()).decode()


def _parse_token_transfer(item: dict[str, Any]) -> OKLinkTokenTransfer:
    return OKLinkTokenTransfer(
        tx_hash=str(item.get("txhash") or item.get("hash") or ""),
        block_number=_int_value(item.get("blockHeight"), 0),
        from_address=str(item.get("from") or ""),
        to_address=str(item.get("to") or ""),
        token_contract=str(item.get("tokenContractAddress") or item.get("address") or ""),
        value=_decimal_or_none(item.get("value")),
    )


def _convert_oklink_log(
    log: dict[str, Any],
    transfer_event_signature: str,
) -> dict[str, Any] | None:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) < 3:
        return None
    if str(topics[0]).lower() != transfer_event_signature.lower():
        return None

    data_value = log.get("data", "0x0")
    if isinstance(data_value, list):
        data = str(data_value[0]) if data_value else "0x0"
    else:
        data = str(data_value or "0x0")

    return {
        "transactionHash": str(log.get("txhash") or log.get("transactionHash") or ""),
        "logIndex": _int_value(log.get("logIndex"), 0),
        "blockNumber": _int_value(log.get("blockHeight") or log.get("blockNumber"), 0),
        "address": _normalize_address(str(log.get("addressEvm") or log.get("address") or "")),
        "topics": [str(topic) for topic in topics],
        "data": data,
    }


def _topic_to_address(topic: Any) -> str:
    value = str(topic)
    return _normalize_address("0x" + value.removeprefix("0x")[-40:])


def _normalize_address(address: str) -> str:
    value = address.strip().lower()
    if value and not value.startswith("0x"):
        return "0x" + value
    return value


def _int_value(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        return int(stripped, 16) if stripped.startswith("0x") else int(stripped)
    return int(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
