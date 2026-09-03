"""Tests for the balance-bisection native deposit scanner."""

from types import SimpleNamespace

import pytest

from src.blockchain.resilient_fetcher import (
    ResilientLogFetcher,
    RpcEndpointSpec,
)

ADDRESS = "0x" + "a" * 40
SENDER = "0x" + "1" * 40


class _FakeEth:
    """Chain where the address balance changes at known blocks."""

    def __init__(self, timeline: dict[int, int], blocks: dict[int, list[dict]]):
        self.timeline = timeline
        self.blocks = blocks
        self.balance_calls = 0
        self.block_calls = 0

    async def get_balance(self, address, block_identifier):
        self.balance_calls += 1
        applicable = [b for b in self.timeline if b <= block_identifier]
        return self.timeline[max(applicable)] if applicable else 0

    async def get_block(self, number, full_transactions=False):
        self.block_calls += 1
        return {"transactions": self.blocks.get(number, [])}


class _FakeWeb3:
    def __init__(self, eth: _FakeEth):
        self.eth = eth

    @staticmethod
    def to_checksum_address(value: str) -> str:
        return value.lower()


def _fetcher(monkeypatch, eth: _FakeEth) -> tuple[ResilientLogFetcher, object]:
    monkeypatch.setattr(
        "src.blockchain.resilient_fetcher.get_chain_config",
        lambda chain: SimpleNamespace(),
    )
    fetcher = ResilientLogFetcher(
        "bsc", [RpcEndpointSpec(url="https://hub.test/rpc/bsc", priority=1)]
    )
    endpoint = fetcher.endpoints[0]
    endpoint.web3 = _FakeWeb3(eth)
    return fetcher, endpoint


def _tx(tx_hash: str, value: int, to: str = ADDRESS) -> dict:
    return {"hash": tx_hash, "from": SENDER, "to": to, "value": value}


@pytest.mark.asyncio
async def test_finds_single_native_deposit(monkeypatch):
    eth = _FakeEth(
        timeline={0: 0, 120: 5 * 10**18},
        blocks={120: [_tx("0x" + "f" * 64, 5 * 10**18)]},
    )
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    transfers = await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=150
    )

    assert len(transfers) == 1
    assert transfers[0].block_number == 120
    assert transfers[0].value_wei == 5 * 10**18
    assert transfers[0].tx_hash == "0x" + "f" * 64
    assert transfers[0].to_address == ADDRESS


@pytest.mark.asyncio
async def test_bisection_beats_linear_scan(monkeypatch):
    """Cost is logarithmic in the window, not linear."""
    eth = _FakeEth(
        timeline={0: 0, 900: 10**18},
        blocks={900: [_tx("0x" + "9" * 64, 10**18)]},
    )
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=1000
    )

    # log2(1000) ~= 10, plus the two boundary reads and one cursor read.
    assert eth.balance_calls < 20
    assert eth.block_calls == 1


@pytest.mark.asyncio
async def test_finds_two_deposits_in_window(monkeypatch):
    eth = _FakeEth(
        timeline={0: 0, 60: 10**18, 130: 3 * 10**18},
        blocks={
            60: [_tx("0x" + "a" * 64, 10**18)],
            130: [_tx("0x" + "b" * 64, 2 * 10**18)],
        },
    )
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    transfers = await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=150
    )

    assert [t.block_number for t in transfers] == [60, 130]
    assert [t.value_wei for t in transfers] == [10**18, 2 * 10**18]


@pytest.mark.asyncio
async def test_no_balance_change_makes_no_block_calls(monkeypatch):
    eth = _FakeEth(timeline={0: 7 * 10**18}, blocks={})
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    transfers = await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=150
    )

    assert transfers == []
    assert eth.block_calls == 0
    assert eth.balance_calls == 2


@pytest.mark.asyncio
async def test_ignores_transactions_to_other_addresses(monkeypatch):
    other = "0x" + "c" * 40
    eth = _FakeEth(
        timeline={0: 0, 100: 10**18},
        blocks={
            100: [
                _tx("0x" + "d" * 64, 5 * 10**18, to=other),
                _tx("0x" + "e" * 64, 10**18),
            ]
        },
    )
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    transfers = await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=150
    )

    assert [t.tx_hash for t in transfers] == ["0x" + "e" * 64]


@pytest.mark.asyncio
async def test_internal_transfer_does_not_loop_forever(monkeypatch):
    """Balance grows with no top-level tx (internal call) — must still terminate."""
    eth = _FakeEth(timeline={0: 0, 100: 10**18}, blocks={100: []})
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    transfers = await fetcher._find_native_transfers_for_address(
        endpoint, ADDRESS, from_block=1, to_block=150
    )

    assert transfers == []


@pytest.mark.asyncio
async def test_scan_reports_failed_addresses(monkeypatch):
    class _FailingEth(_FakeEth):
        async def get_balance(self, address, block_identifier):
            raise RuntimeError("upstream down")

    eth = _FailingEth(timeline={}, blocks={})
    fetcher, endpoint = _fetcher(monkeypatch, eth)

    result = await fetcher.fetch_native_transfers(
        from_block=1, to_block=150, to_addresses=[ADDRESS]
    )

    assert result.is_complete is False
    assert result.failed_address_count == 1
    assert result.transfers == []


@pytest.mark.asyncio
async def test_scan_without_addresses_is_a_noop(monkeypatch):
    eth = _FakeEth(timeline={0: 0}, blocks={})
    fetcher, _ = _fetcher(monkeypatch, eth)

    result = await fetcher.fetch_native_transfers(
        from_block=1, to_block=150, to_addresses=[]
    )

    assert result.is_complete is True
    assert result.transfers == []
    assert eth.balance_calls == 0
