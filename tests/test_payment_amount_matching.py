"""What the scanner does when the transfer does not match the invoice."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.workers.evm_log_poller as poller

ADDRESS = "0x" + "a" * 40
USDT = "0x" + "b" * 40
USDC = "0x" + "c" * 40
SENDER = "0x" + "1" * 40
INVOICE_AMOUNT = Decimal("100")


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Adapter:
    async def get_latest_block_number(self):
        return 150

    async def get_transfer_logs_batch(self, *args, **kwargs):
        raise AssertionError("resilient fetcher must be used")


@dataclass
class _FetchResult:
    logs: list[dict]
    is_complete: bool = True
    failed_address_count: int = 0
    method_used: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(value="or_topics")
    )
    rpc_used: str = "https://hub.test/rpc/bsc"
    latency_ms: float = 5.0


class _FakeFetcher:
    def __init__(self, logs):
        self.result = _FetchResult(logs=logs)

    async def fetch_transfer_logs(self, **kwargs):
        return self.result

    async def fetch_native_transfers(self, **kwargs):
        return SimpleNamespace(
            transfers=[], is_complete=True, failed_address_count=0
        )


def _chain_config():
    tokens = {
        "USDT": SimpleNamespace(symbol="USDT", contract_address=USDT, decimals=18),
        "USDC": SimpleNamespace(symbol="USDC", contract_address=USDC, decimals=18),
    }
    return SimpleNamespace(
        scanner_provider="rpc",
        scan_window=100,
        reorg_buffer=0,
        block_time_sec=2,
        native_symbol="BNB",
        native_decimals=18,
        tokens=tokens,
        is_native_asset=lambda symbol: symbol.upper() == "BNB",
        get_asset_contract=lambda symbol: (
            poller.NATIVE_TOKEN_CONTRACT
            if symbol.upper() == "BNB"
            else tokens[symbol.upper()].contract_address
        ),
    )


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _transfer_log(amount: Decimal, token: str = USDT) -> dict:
    raw = int(amount * Decimal(10**18))
    return {
        "topics": [
            "0x" + "d" * 64,
            _topic(SENDER),
            _topic(ADDRESS),
        ],
        "data": hex(raw),
        "address": token,
        "transactionHash": "0x" + "f" * 64,
        "logIndex": 0,
        "blockNumber": 120,
    }


def _payment_session():
    return SimpleNamespace(
        token="USDT",
        invoice=SimpleNamespace(
            amount=INVOICE_AMOUNT,
            created_at=datetime.now(UTC),
        ),
        deposit_address=SimpleNamespace(address=ADDRESS),
        received_amount=None,
        mismatch_reason=None,
        mismatch_token=None,
    )


def _patch(monkeypatch, session, payment_session, logs):
    @asynccontextmanager
    async def _session_context():
        yield session

    async def _active_addresses(db_session, chain):
        return {ADDRESS: payment_session}

    async def _checkpoint(db_session, chain, adapter, earliest):
        return 100

    async def _update_checkpoint(db_session, chain, block_number):
        return None

    monkeypatch.setattr(poller, "get_session_context", _session_context)
    monkeypatch.setattr(poller, "get_active_deposit_addresses", _active_addresses)
    monkeypatch.setattr(poller, "get_chain_config", lambda chain: _chain_config())
    monkeypatch.setattr(poller, "get_evm_adapter", lambda chain: _Adapter())
    monkeypatch.setattr(poller, "get_or_create_checkpoint", _checkpoint)
    monkeypatch.setattr(poller, "update_checkpoint", _update_checkpoint)
    monkeypatch.setattr(poller, "get_resilient_fetcher", lambda chain: _FakeFetcher(logs))
    monkeypatch.setattr(
        poller, "_parse_transfer_log", lambda chain, log: _real_parse(log)
    )


def _real_parse(log: dict):
    raw = int(log["data"], 16)
    return poller.TransferLog(
        tx_hash=log["transactionHash"],
        log_index=log["logIndex"],
        block_number=log["blockNumber"],
        from_address=SENDER,
        to_address=ADDRESS,
        token_contract=log["address"].lower(),
        amount=Decimal(raw) / Decimal(10**18),
    )


# === tolerance ===


def test_tolerance_uses_percent_on_large_amounts():
    # 1% of 100 = 1.0, which beats the 0.02 absolute floor
    assert poller._underpayment_tolerance(Decimal("100")) == Decimal("1.000000")


def test_tolerance_uses_absolute_floor_on_small_amounts():
    # 1% of 1 = 0.01, below the 0.02 floor
    assert poller._underpayment_tolerance(Decimal("1")) == Decimal("0.020000")


# === scanner behaviour ===


@pytest.mark.asyncio
async def test_exact_amount_is_accepted(monkeypatch):
    session, ps = _Session(), _payment_session()
    _patch(monkeypatch, session, ps, [_transfer_log(INVOICE_AMOUNT)])

    await poller.poll_chain("bsc")

    assert ps.received_amount == INVOICE_AMOUNT
    assert ps.mismatch_reason is None


@pytest.mark.asyncio
async def test_underpayment_within_tolerance_is_accepted(monkeypatch):
    """Exchanges shave a withdrawal fee; 99.5 of 100 must still pay the invoice."""
    session, ps = _Session(), _payment_session()
    _patch(monkeypatch, session, ps, [_transfer_log(Decimal("99.5"))])

    await poller.poll_chain("bsc")

    assert ps.received_amount == Decimal("99.5")
    assert ps.mismatch_reason is None


@pytest.mark.asyncio
async def test_underpayment_beyond_tolerance_is_recorded_not_dropped(monkeypatch):
    session, ps = _Session(), _payment_session()
    _patch(monkeypatch, session, ps, [_transfer_log(Decimal("80"))])

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason == poller.MISMATCH_UNDERPAID
    assert ps.received_amount == Decimal("80")


@pytest.mark.asyncio
async def test_overpayment_is_accepted_at_the_amount_that_arrived(monkeypatch):
    session, ps = _Session(), _payment_session()
    _patch(monkeypatch, session, ps, [_transfer_log(Decimal("120"))])

    await poller.poll_chain("bsc")

    assert ps.received_amount == Decimal("120")
    assert ps.mismatch_reason is None


@pytest.mark.asyncio
async def test_wrong_token_is_recorded_with_its_symbol(monkeypatch):
    """USDC sent against a USDT invoice must not vanish silently."""
    session, ps = _Session(), _payment_session()
    _patch(monkeypatch, session, ps, [_transfer_log(INVOICE_AMOUNT, token=USDC)])

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason == poller.MISMATCH_WRONG_TOKEN
    assert ps.mismatch_token == "USDC"
    assert ps.received_amount == INVOICE_AMOUNT


def test_token_symbol_lookup_falls_back_to_contract():
    config = _chain_config()
    unknown = "0x" + "9" * 40
    assert poller._token_symbol_for_contract(config, USDT) == "USDT"
    assert poller._token_symbol_for_contract(config, unknown) == unknown
    assert (
        poller._token_symbol_for_contract(config, poller.NATIVE_TOKEN_CONTRACT) == "BNB"
    )
