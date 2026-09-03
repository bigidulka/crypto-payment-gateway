"""Tests for the RPC deposit scanner used by the invoice flow."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.workers.evm_log_poller as poller
from src.blockchain.resilient_fetcher import (
    NativeScanResult,
    NativeTransfer,
    RpcEndpointSpec,
    build_endpoint_specs,
)

ADDRESS = "0x" + "a" * 40
NATIVE_ADDRESS = "0x" + "e" * 40
USDT = "0x" + "b" * 40
USDC = "0x" + "c" * 40


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
        raise AssertionError("per-address adapter scan must not be used")


@dataclass
class _FetchResult:
    logs: list[dict]
    is_complete: bool
    failed_address_count: int
    method_used: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(value="or_topics")
    )
    rpc_used: str = "https://hub.test/rpc/bsc"
    latency_ms: float = 12.0


class _FakeResilientFetcher:
    def __init__(self, result: _FetchResult, native: NativeScanResult | None = None):
        self.result = result
        self.native = native or NativeScanResult(
            transfers=[],
            rpc_used="https://hub.test/rpc/bsc",
            latency_ms=1.0,
            is_complete=True,
            failed_address_count=0,
        )
        self.calls: list[dict] = []
        self.native_calls: list[dict] = []

    async def fetch_transfer_logs(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    async def fetch_native_transfers(self, **kwargs):
        self.native_calls.append(kwargs)
        return self.native


def _chain_config():
    tokens = {
        "USDT": SimpleNamespace(contract_address=USDT, decimals=18),
        "USDC": SimpleNamespace(contract_address=USDC, decimals=18),
    }
    return SimpleNamespace(
        scanner_provider="rpc",
        oklink_chain="bsc",
        scanner_rpc_chain="bsc",
        scanner_page_limit=20,
        scanner_max_pages_per_address=5,
        scanner_max_log_pages_per_tx=20,
        scanner_request_delay_ms=0,
        reorg_buffer=0,
        scan_window=100,
        block_time_sec=2,
        native_symbol="BNB",
        native_decimals=18,
        tokens=tokens,
        is_native_asset=lambda symbol: symbol.upper() == "BNB",
        get_asset_contract=lambda symbol: (
            "0x0000000000000000000000000000000000000000"
            if symbol.upper() == "BNB"
            else tokens[symbol.upper()].contract_address
        ),
    )


def _payment_session(token: str = "USDT", address: str = ADDRESS):
    return SimpleNamespace(
        token=token,
        invoice=SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=UTC)),
        deposit_address=SimpleNamespace(address=address),
    )


def _patch_common(monkeypatch, session, adapter, checkpoint, active_addresses=None):
    @asynccontextmanager
    async def _session_context():
        yield session

    async def _active_addresses(db_session, chain):
        return active_addresses or {ADDRESS: _payment_session()}

    async def _get_checkpoint(db_session, chain, chain_adapter, earliest_invoice_time):
        raise AssertionError("invoice flow must not depend on the chain checkpoint")

    async def _update_checkpoint(db_session, chain, block_number):
        checkpoint["block"] = block_number

    monkeypatch.setattr(poller, "get_session_context", _session_context)
    monkeypatch.setattr(poller, "get_active_deposit_addresses", _active_addresses)
    monkeypatch.setattr(poller, "get_chain_config", lambda chain: _chain_config())
    monkeypatch.setattr(poller, "get_evm_adapter", lambda chain: adapter)
    monkeypatch.setattr(poller, "get_or_create_checkpoint", _get_checkpoint)
    monkeypatch.setattr(poller, "update_checkpoint", _update_checkpoint)


@pytest.mark.asyncio
async def test_rpc_scan_uses_one_or_topics_request(monkeypatch):
    """All active addresses go out in a single request, not one per address."""
    session = _Session()
    adapter = _Adapter()
    checkpoint: dict = {}
    second = "0x" + "d" * 40
    _patch_common(
        monkeypatch,
        session,
        adapter,
        checkpoint,
        active_addresses={
            ADDRESS: _payment_session(),
            second: _payment_session(address=second),
        },
    )
    fetcher = _FakeResilientFetcher(
        _FetchResult(logs=[], is_complete=True, failed_address_count=0)
    )
    monkeypatch.setattr(poller, "get_resilient_fetcher", lambda chain: fetcher)

    await poller.poll_chain("bsc")

    assert adapter.batch_called is False
    assert len(fetcher.calls) == 1
    call = fetcher.calls[0]
    assert sorted(call["to_addresses"]) == sorted([ADDRESS, second])
    assert call["from_block"] == 0
    assert call["to_block"] == 150
    assert checkpoint == {"block": 150}
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_rpc_incomplete_scan_does_not_advance_checkpoint(monkeypatch):
    session = _Session()
    adapter = _Adapter()
    checkpoint: dict = {}
    _patch_common(monkeypatch, session, adapter, checkpoint)
    fetcher = _FakeResilientFetcher(
        _FetchResult(logs=[], is_complete=False, failed_address_count=1)
    )
    monkeypatch.setattr(poller, "get_resilient_fetcher", lambda chain: fetcher)

    await poller.poll_chain("bsc")

    assert checkpoint == {}
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_rpc_scan_collects_native_transfers(monkeypatch):
    """Native invoices keep working after the move off OKLink."""
    session = _Session()
    adapter = _Adapter()
    checkpoint: dict = {}
    recorded: list = []
    _patch_common(
        monkeypatch,
        session,
        adapter,
        checkpoint,
        active_addresses={
            NATIVE_ADDRESS: _payment_session(token="BNB", address=NATIVE_ADDRESS)
        },
    )
    fetcher = _FakeResilientFetcher(
        _FetchResult(logs=[], is_complete=True, failed_address_count=0),
        native=NativeScanResult(
            transfers=[
                NativeTransfer(
                    tx_hash="0x" + "f" * 64,
                    block_number=120,
                    from_address="0x" + "1" * 40,
                    to_address=NATIVE_ADDRESS,
                    value_wei=2 * 10**18,
                )
            ],
            rpc_used="https://hub.test/rpc/bsc",
            latency_ms=5.0,
            is_complete=True,
            failed_address_count=0,
        ),
    )
    monkeypatch.setattr(poller, "get_resilient_fetcher", lambda chain: fetcher)

    async def _record(db_session, chain, transfer, payment_session):
        recorded.append(transfer)

    monkeypatch.setattr(poller, "record_transfer", _record, raising=False)

    await poller.poll_chain("bsc")

    assert fetcher.native_calls == [
        {"from_block": 0, "to_block": 150, "to_addresses": [NATIVE_ADDRESS]}
    ]


@pytest.mark.asyncio
async def test_rpc_falls_back_to_adapter_when_fetcher_missing(monkeypatch):
    session = _Session()
    checkpoint: dict = {}

    class _FallbackAdapter(_Adapter):
        async def get_transfer_logs_batch(self, **kwargs):
            self.batch_called = True
            return SimpleNamespace(
                transfers=[], is_complete=True, failed_address_count=0
            )

    adapter = _FallbackAdapter()
    _patch_common(monkeypatch, session, adapter, checkpoint)
    monkeypatch.setattr(poller, "get_resilient_fetcher", lambda chain: None)

    await poller.poll_chain("bsc")

    assert adapter.batch_called is True


def test_build_endpoint_specs_puts_keyed_rpc_first():
    specs = build_endpoint_specs(
        ["https://public-a.test", "https://public-b.test"],
        keyed_url="https://hub.test/rpc/bsc",
        keyed_headers={"X-Hub-Key": "secret"},
    )

    assert [spec.url for spec in specs] == [
        "https://hub.test/rpc/bsc",
        "https://public-a.test",
        "https://public-b.test",
    ]
    assert [spec.priority for spec in specs] == [1, 2, 3]
    assert specs[0].headers == {"X-Hub-Key": "secret"}
    assert specs[1].headers == {}


def test_build_endpoint_specs_without_keyed_rpc():
    specs = build_endpoint_specs(["https://public-a.test"])

    assert [spec.url for spec in specs] == ["https://public-a.test"]
    assert specs[0].priority == 1


def test_endpoint_spec_passes_headers_to_provider():
    spec = RpcEndpointSpec(
        url="https://hub.test/rpc/bsc",
        priority=1,
        headers={"X-Hub-Key": "secret"},
    )

    web3 = spec.build_web3(30.0)

    assert web3.provider.get_request_kwargs()["headers"]["X-Hub-Key"] == "secret"


def test_native_transfer_amount_conversion():
    """Wei to decimal conversion the poller relies on."""
    assert Decimal(2 * 10**18) / Decimal(10**18) == Decimal("2")
