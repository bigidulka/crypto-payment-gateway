import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from src.db.models import Merchant, UserWallet, WalletAddress
from src.workers.persistent_poller import poll_persistent_deposits


class _Adapter:
    async def get_latest_block_number(self):
        return 150


class _FakeSession:
    def __init__(self):
        self.objects = []

    def add(self, obj):
        self.objects.append(obj)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def refresh(self, obj):
        return None

    async def execute(self, stmt):
        for obj in self.objects:
            if isinstance(obj, WalletAddress):
                obj.last_scanned_block = 150
        return None


@dataclass(frozen=True)
class _Method:
    value: str


@dataclass(frozen=True)
class _FetchResult:
    logs: list[dict]
    method_used: _Method
    rpc_used: str
    latency_ms: float
    from_block: int
    to_block: int
    is_complete: bool
    failed_address_count: int


class _FakeFetcher:
    def __init__(self, result: _FetchResult):
        self._result = result
        self.closed = False

    async def fetch_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
    ) -> _FetchResult:
        return self._result

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def test_session(monkeypatch):
    session = _FakeSession()

    @asynccontextmanager
    async def _session_context():
        yield session

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_session_context",
        _session_context,
    )
    return session


async def _create_wallet_address(test_session, chain: str = "bsc") -> WalletAddress:
    merchant = Merchant(
        id=uuid.uuid4(),
        name="Test Merchant",
        email=f"merchant-{uuid.uuid4().hex[:8]}@example.com",
        is_active=True,
    )
    test_session.add(merchant)

    wallet = UserWallet(
        id=uuid.uuid4(),
        merchant_id=merchant.id,
        external_user_id=f"user-{uuid.uuid4().hex[:8]}",
    )
    test_session.add(wallet)

    address = WalletAddress(
        id=uuid.uuid4(),
        user_wallet_id=wallet.id,
        chain=chain,
        address="0x" + "a" * 40,
        derivation_index=1,
        encrypted_private_key="enc",
        is_active=True,
        last_scanned_block=100,
        created_at=datetime.now(timezone.utc),
    )
    test_session.add(address)
    await test_session.commit()

    return address


def _chain_config():
    return type(
        "Cfg",
        (),
        {
            "scanner_provider": "oklink",
            "oklink_chain": "bsc",
            "scanner_page_limit": 20,
            "scanner_max_pages_per_address": 5,
            "scanner_max_log_pages_per_tx": 20,
            "scanner_request_delay_ms": 200,
            "reorg_buffer": 0,
            "scan_window": 100,
            "tokens": {
                "USDT": type("T", (), {"contract_address": "0x" + "b" * 40})(),
                "USDC": type("T", (), {"contract_address": "0x" + "c" * 40})(),
            },
            "confirmations": 12,
            "block_time_sec": 2,
        },
    )()


def _patch_common(monkeypatch, address: WalletAddress) -> None:
    async def _active_addresses(session, chain):
        return {address.address.lower(): address}

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_chain_config",
        lambda chain: _chain_config(),
    )
    monkeypatch.setattr(
        "src.workers.persistent_poller.get_all_active_wallet_addresses",
        _active_addresses,
    )
    monkeypatch.setattr(
        "src.workers.persistent_poller.get_evm_adapter",
        lambda chain: _Adapter(),
    )
    monkeypatch.setattr(
        "src.workers.persistent_poller.get_resilient_fetcher",
        lambda chain: (_ for _ in ()).throw(AssertionError("RPC fetcher must not be used")),
    )


@pytest.mark.asyncio
async def test_oklink_provider_advances_checkpoint_without_rpc_log_fallback(
    test_session,
    monkeypatch,
):
    address = await _create_wallet_address(test_session)
    _patch_common(monkeypatch, address)
    fetcher = _FakeFetcher(
        _FetchResult(
            logs=[],
            method_used=_Method("oklink_address_token_transfers"),
            rpc_used="oklink",
            latency_ms=10.0,
            from_block=101,
            to_block=150,
            is_complete=True,
            failed_address_count=0,
        )
    )
    monkeypatch.setattr(
        "src.workers.persistent_poller._build_oklink_fetcher",
        lambda chain, config: fetcher,
    )

    class _NoopWalletService:
        def __init__(self, session):
            self.session = session

        async def record_deposit(
            self,
            wallet_address,
            tx_hash,
            block_number,
            log_index,
            amount,
            asset,
            token_contract,
            from_address,
            required_confirmations,
        ):
            return None

    monkeypatch.setattr("src.workers.persistent_poller.UserWalletService", _NoopWalletService)

    result = await poll_persistent_deposits("bsc")

    assert result.is_complete is True
    assert result.fetch_is_complete is True
    assert result.checkpoint_advanced is True
    assert fetcher.closed is True

    await test_session.refresh(address)
    assert address.last_scanned_block == 150


@pytest.mark.asyncio
async def test_oklink_provider_error_does_not_fallback_to_rpc_logs(
    test_session,
    monkeypatch,
):
    address = await _create_wallet_address(test_session)
    _patch_common(monkeypatch, address)

    class _FailingFetcher:
        def __init__(self):
            self.closed = False

        async def fetch_transfer_logs(
            self,
            from_block: int,
            to_block: int,
            to_addresses: list[str],
            token_contracts: list[str],
        ):
            raise RuntimeError("oklink down")

        async def aclose(self) -> None:
            self.closed = True

    fetcher = _FailingFetcher()
    monkeypatch.setattr(
        "src.workers.persistent_poller._build_oklink_fetcher",
        lambda chain, config: fetcher,
    )

    result = await poll_persistent_deposits("bsc")

    assert result.is_complete is False
    assert result.fetch_is_complete is False
    assert result.checkpoint_advanced is False
    assert fetcher.closed is True

    await test_session.refresh(address)
    assert address.last_scanned_block == 100
