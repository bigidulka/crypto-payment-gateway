from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import src.workers.evm_log_poller as poller


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
        self.closed = False

    async def fetch_transfer_logs(self, **kwargs):
        return self.result

    async def aclose(self):
        self.closed = True


def _chain_config():
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
        tokens={
            "USDT": SimpleNamespace(
                contract_address="0x" + "b" * 40,
                decimals=18,
            ),
            "USDC": SimpleNamespace(
                contract_address="0x" + "c" * 40,
                decimals=18,
            ),
        },
    )


def _payment_session():
    return SimpleNamespace(
        invoice=SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=UTC)),
        deposit_address=SimpleNamespace(address="0x" + "a" * 40),
    )


def _patch_common(monkeypatch, session: _Session, adapter: _Adapter, checkpoint):
    @asynccontextmanager
    async def _session_context():
        yield session

    async def _active_addresses(db_session, chain):
        return {"0x" + "a" * 40: _payment_session()}

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
async def test_oklink_active_check_scan_advances_checkpoint_without_rpc_fallback(
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
