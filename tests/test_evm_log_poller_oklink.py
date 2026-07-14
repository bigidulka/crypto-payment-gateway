import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

import src.workers.evm_log_poller as poller
from src.blockchain.oklink_client import (
    OKLinkClientConfig,
    OKLinkExplorerClient,
    OKLinkTransferLogFetcher,
)


class _Session:
    def __init__(self):
        self.rollback_count = 0

    async def commit(self):
        return None

    async def rollback(self):
        self.rollback_count += 1


class _Adapter:
    def __init__(self):
        self.batch_called = False

    async def get_latest_block_number(self):
        return 150

    async def get_transfer_logs_batch(self, *args, **kwargs):
        self.batch_called = True
        raise AssertionError("RPC log fallback must not be used for OKLink provider")


@dataclass(frozen=True)
class _FetchResult:
    logs: list[dict]
    is_complete: bool
    failed_address_count: int


class _FakeFetcher:
    def __init__(self, result: _FetchResult):
        self.result = result
        self.chain = "bsc"
        self.closed = False
        self.calls = []
        self.native_calls = []
        self.client = SimpleNamespace(
            config=SimpleNamespace(request_timeout_seconds=0.01),
            fetch_address_transactions=self.fetch_address_transactions,
        )

    async def fetch_transfer_logs(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    async def fetch_address_transactions(self, chain, address):
        self.native_calls.append((chain, address))
        return []

    async def aclose(self):
        self.closed = True


class _DelayedCloseFetcher(_FakeFetcher):
    def __init__(self, result: _FetchResult):
        super().__init__(result)
        self.close_started = False

    async def aclose(self):
        self.close_started = True
        await asyncio.sleep(0.03)
        self.closed = True


def _oklink_fetcher(
    handler,
    *,
    page_limit: int = 20,
    request_delay_seconds: float = 0,
) -> tuple[OKLinkTransferLogFetcher, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://oklink.test",
    )
    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url="https://oklink.test",
            api_prefix="/api/explorer/",
            referer="https://oklink.test/bsc",
            user_agent="pytest",
            web_key="abcdefgh12345678",
            transfer_event_signature="0x" + "d" * 64,
            page_limit=page_limit,
            request_timeout_seconds=0.01,
            request_delay_seconds=request_delay_seconds,
            max_pages_per_address=2,
            max_log_pages_per_tx=2,
            api_key_time_shift_ms=1111111111111,
        ),
        http_client,
    )
    return OKLinkTransferLogFetcher("bsc", client), http_client


def _chain_config():
    tokens = {
        "USDT": SimpleNamespace(
            contract_address="0x" + "b" * 40,
            decimals=18,
        ),
        "USDC": SimpleNamespace(
            contract_address="0x" + "c" * 40,
            decimals=18,
        ),
    }
    return SimpleNamespace(
        scanner_provider="oklink",
        oklink_chain="bsc",
        scanner_page_limit=20,
        scanner_max_pages_per_address=5,
        scanner_max_log_pages_per_tx=20,
        scanner_request_delay_ms=0,
        reorg_buffer=0,
        scan_window=100,
        block_time_sec=2,
        native_symbol="BNB",
        tokens=tokens,
        is_native_asset=lambda symbol: symbol.upper() == "BNB",
        get_asset_contract=lambda symbol: (
            "0x0000000000000000000000000000000000000000"
            if symbol.upper() == "BNB"
            else tokens[symbol.upper()].contract_address
        ),
    )


def _payment_session(token: str = "USDT", address: str = "0x" + "a" * 40):
    return SimpleNamespace(
        token=token,
        invoice=SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=UTC)),
        deposit_address=SimpleNamespace(address=address),
    )


def _patch_common(
    monkeypatch,
    session: _Session,
    adapter: _Adapter,
    checkpoint,
    active_addresses=None,
):
    @asynccontextmanager
    async def _session_context():
        yield session

    async def _active_addresses(db_session, chain):
        return active_addresses or {"0x" + "a" * 40: _payment_session()}

    async def _get_checkpoint(db_session, chain, chain_adapter, earliest_invoice_time):
        return 100

    async def _update_checkpoint(db_session, chain, block_number):
        checkpoint["block"] = block_number

    monkeypatch.setattr(poller, "get_session_context", _session_context)
    monkeypatch.setattr(poller, "get_active_deposit_addresses", _active_addresses)
    monkeypatch.setattr(poller, "get_chain_config", lambda chain: _chain_config())
    monkeypatch.setattr(poller, "get_evm_adapter", lambda chain: adapter)
    monkeypatch.setattr(poller, "get_or_create_checkpoint", _get_checkpoint)
    monkeypatch.setattr(poller, "update_checkpoint", _update_checkpoint)


@pytest.mark.asyncio
async def test_oklink_active_check_scan_ignores_stale_checkpoint(
    monkeypatch,
):
    session = _Session()
    adapter = _Adapter()
    checkpoint = {}
    _patch_common(monkeypatch, session, adapter, checkpoint)
    fetcher = _FakeFetcher(
        _FetchResult(logs=[], is_complete=True, failed_address_count=0)
    )
    monkeypatch.setattr(poller, "_build_oklink_fetcher", lambda chain, config: fetcher)

    await poller.poll_chain("bsc")

    assert adapter.batch_called is False
    assert fetcher.closed is True
    assert checkpoint == {"block": 150}
    assert fetcher.calls == [
        {
            "from_block": 0,
            "to_block": 150,
            "to_addresses": ["0x" + "a" * 40],
            "token_contracts": ["0x" + "b" * 40, "0x" + "c" * 40],
        }
    ]
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_oklink_active_check_incomplete_scan_does_not_advance_checkpoint(
    monkeypatch,
):
    session = _Session()
    adapter = _Adapter()
    checkpoint = {}
    _patch_common(monkeypatch, session, adapter, checkpoint)
    fetcher = _FakeFetcher(
        _FetchResult(logs=[], is_complete=False, failed_address_count=1)
    )
    monkeypatch.setattr(poller, "_build_oklink_fetcher", lambda chain, config: fetcher)

    await poller.poll_chain("bsc")

    assert adapter.batch_called is False
    assert fetcher.closed is True
    assert checkpoint == {}
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_oklink_cleanup_deadline_is_best_effort(monkeypatch, caplog):
    session = _Session()
    adapter = _Adapter()
    checkpoint = {}
    _patch_common(monkeypatch, session, adapter, checkpoint)
    fetcher = _DelayedCloseFetcher(
        _FetchResult(logs=[], is_complete=True, failed_address_count=0)
    )
    monkeypatch.setattr(poller, "_build_oklink_fetcher", lambda chain, config: fetcher)
    caplog.set_level(logging.WARNING, logger="src.workers.evm_log_poller")

    await asyncio.wait_for(poller.poll_chain("bsc"), timeout=0.5)

    assert adapter.batch_called is False
    assert checkpoint == {"block": 150}
    assert fetcher.close_started is True
    await asyncio.sleep(0.04)
    assert fetcher.closed is True
    assert "OKLink fetcher cleanup still pending" in caplog.text


@pytest.mark.asyncio
async def test_oklink_scan_allows_request_pacing_longer_than_request_timeout(monkeypatch):
    session = _Session()
    adapter = _Adapter()
    checkpoint = {}
    address = "0x" + "a" * 40
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["offset"])
        if body["offset"] == "0":
            hits = [
                {
                    "txhash": "0x" + "1" * 64,
                    "blockHeight": 100,
                    "from": "0x" + "2" * 40,
                    "to": address,
                    "tokenContractAddress": "0x" + "f" * 40,
                    "value": 1,
                }
            ]
        else:
            hits = []
        return httpx.Response(200, json={"code": 0, "data": {"hits": hits}})

    _patch_common(monkeypatch, session, adapter, checkpoint)
    fetcher, http_client = _oklink_fetcher(
        handler,
        page_limit=1,
        request_delay_seconds=0.02,
    )
    monkeypatch.setattr(poller, "_build_oklink_fetcher", lambda chain, config: fetcher)

    await asyncio.wait_for(poller.poll_chain("bsc"), timeout=0.5)
    await http_client.aclose()

    assert checkpoint == {"block": 150}
    assert offsets == ["0", "1"]


@pytest.mark.asyncio
async def test_oklink_native_request_timeout_continues_other_native_scans_without_checkpoint_advance(
    monkeypatch,
    caplog,
):
    session = _Session()
    adapter = _Adapter()
    checkpoint = {}
    first_native_address = "0x" + "d" * 40
    second_native_address = "0x" + "e" * 40
    native_requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        native_requests.append(request.url.path)
        if request.url.path.endswith(
            f"/addresses/{first_native_address}/transactionsByClassfy/condition"
        ):
            await asyncio.Event().wait()
        return httpx.Response(200, json={"code": 0, "data": {"hits": []}})

    _patch_common(
        monkeypatch,
        session,
        adapter,
        checkpoint,
        {
            first_native_address: _payment_session("BNB", first_native_address),
            second_native_address: _payment_session("BNB", second_native_address),
        },
    )
    fetcher, http_client = _oklink_fetcher(handler)
    monkeypatch.setattr(poller, "_build_oklink_fetcher", lambda chain, config: fetcher)
    caplog.set_level(logging.WARNING)

    await asyncio.wait_for(poller.poll_chain("bsc"), timeout=0.5)
    await http_client.aclose()

    assert adapter.batch_called is False
    assert checkpoint == {}
    assert native_requests == [
        f"/api/explorer/v2/bsc/addresses/{first_native_address}/transactionsByClassfy/condition",
        f"/api/explorer/v2/bsc/addresses/{second_native_address}/transactionsByClassfy/condition",
    ]
    assert "OKLink request timed out" in caplog.text
    assert "Failed to fetch native OKLink txs" in caplog.text
