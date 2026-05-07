import uuid
from datetime import datetime, timezone
import pytest

from src.blockchain.resilient_fetcher import FetchMethod, TransferLogResult
from src.db.models import Merchant, UserWallet, WalletAddress
from src.workers.persistent_poller import poll_persistent_deposits


class _Adapter:
    async def get_latest_block_number(self):
        return 150


def _fake_get_all_active_wallet_addresses_factory(address: WalletAddress):
    async def _fake_get_all_active_wallet_addresses(session, chain):
        return {address.address.lower(): address}

    return _fake_get_all_active_wallet_addresses


class _FakeFetcher:
    def __init__(self, result: TransferLogResult):
        self._result = result

    async def fetch_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
    ) -> TransferLogResult:
        return self._result


async def _create_wallet_address(test_session, chain: str = "arbitrum") -> WalletAddress:
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


@pytest.mark.asyncio
async def test_checkpoint_not_advanced_on_partial_fetch(test_session, monkeypatch):
    address = await _create_wallet_address(test_session)

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_chain_config",
        lambda chain: type(
            "Cfg",
            (),
            {
                "reorg_buffer": 0,
                "scan_window": 100,
                "tokens": {
                    "USDT": type(
                        "T", (), {"contract_address": "0x" + "b" * 40, "decimals": 6}
                    )(),
                    "USDC": type(
                        "T", (), {"contract_address": "0x" + "c" * 40, "decimals": 6}
                    )(),
                },
                "confirmations": 12,
                "block_time_sec": 2,
            },
        )(),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_all_active_wallet_addresses",
        _fake_get_all_active_wallet_addresses_factory(address),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_evm_adapter",
        lambda chain: _Adapter(),
    )

    fetcher = _FakeFetcher(
        TransferLogResult(
            logs=[],
            method_used=FetchMethod.PARALLEL_BATCH,
            rpc_used="rpc://test",
            latency_ms=5.0,
            from_block=101,
            to_block=150,
            is_complete=False,
            failed_address_count=1,
        )
    )

    result = await poll_persistent_deposits("arbitrum", fetcher_override=fetcher)

    assert result.is_complete is False
    assert result.fetch_is_complete is False
    assert result.failed_address_count == 1
    assert result.checkpoint_advanced is False

    await test_session.refresh(address)
    assert address.last_scanned_block == 100


@pytest.mark.asyncio
async def test_checkpoint_not_advanced_on_record_error(test_session, monkeypatch):
    address = await _create_wallet_address(test_session)

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_chain_config",
        lambda chain: type(
            "Cfg",
            (),
            {
                "reorg_buffer": 0,
                "scan_window": 100,
                "tokens": {
                    "USDT": type(
                        "T", (), {"contract_address": "0x" + "b" * 40, "decimals": 6}
                    )(),
                    "USDC": type(
                        "T", (), {"contract_address": "0x" + "c" * 40, "decimals": 6}
                    )(),
                },
                "confirmations": 12,
                "block_time_sec": 2,
            },
        )(),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_all_active_wallet_addresses",
        _fake_get_all_active_wallet_addresses_factory(address),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_evm_adapter",
        lambda chain: _Adapter(),
    )

    raw_log = {
        "transactionHash": "0x" + "1" * 64,
        "logIndex": 1,
        "blockNumber": 120,
        "address": "0x" + "b" * 40,
        "topics": [
            "0xddf252ad00000000000000000000000000000000000000000000000000000000",
            "0x" + "0" * 24 + "2" * 40,
            "0x" + "0" * 24 + address.address.lower().replace("0x", ""),
        ],
        "data": hex(1_000_000),
    }

    fetcher = _FakeFetcher(
        TransferLogResult(
            logs=[raw_log],
            method_used=FetchMethod.OR_TOPICS,
            rpc_used="rpc://test",
            latency_ms=8.0,
            from_block=101,
            to_block=150,
            is_complete=True,
            failed_address_count=0,
        )
    )

    class _FailingWalletService:
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
            raise RuntimeError("record failed")

    monkeypatch.setattr(
        "src.workers.persistent_poller.UserWalletService", _FailingWalletService
    )

    result = await poll_persistent_deposits("arbitrum", fetcher_override=fetcher)

    assert result.is_complete is False
    assert result.fetch_is_complete is True
    assert result.record_error_count == 1
    assert result.checkpoint_advanced is False

    await test_session.refresh(address)
    assert address.last_scanned_block == 100


@pytest.mark.asyncio
async def test_checkpoint_advanced_on_complete_scan(test_session, monkeypatch):
    address = await _create_wallet_address(test_session)

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_chain_config",
        lambda chain: type(
            "Cfg",
            (),
            {
                "reorg_buffer": 0,
                "scan_window": 100,
                "tokens": {
                    "USDT": type(
                        "T", (), {"contract_address": "0x" + "b" * 40, "decimals": 6}
                    )(),
                    "USDC": type(
                        "T", (), {"contract_address": "0x" + "c" * 40, "decimals": 6}
                    )(),
                },
                "confirmations": 12,
                "block_time_sec": 2,
            },
        )(),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_all_active_wallet_addresses",
        _fake_get_all_active_wallet_addresses_factory(address),
    )

    monkeypatch.setattr(
        "src.workers.persistent_poller.get_evm_adapter",
        lambda chain: _Adapter(),
    )

    fetcher = _FakeFetcher(
        TransferLogResult(
            logs=[],
            method_used=FetchMethod.OR_TOPICS,
            rpc_used="rpc://test",
            latency_ms=4.0,
            from_block=101,
            to_block=150,
            is_complete=True,
            failed_address_count=0,
        )
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

    result = await poll_persistent_deposits("arbitrum", fetcher_override=fetcher)

    assert result.is_complete is True
    assert result.fetch_is_complete is True
    assert result.record_error_count == 0
    assert result.checkpoint_advanced is True

    await test_session.refresh(address)
    assert address.last_scanned_block == 150
