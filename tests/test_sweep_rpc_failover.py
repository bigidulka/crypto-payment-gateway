"""
Что делает sweep, когда один RPC-endpoint запрещает метод.

Живой случай: bsc-rpc.publicnode.com отвечает 403 на eth_getTransactionReceipt,
остальные три endpoint'а сети отвечают нормально. Адаптер читал receipt мимо
ротации, поэтому sweep на BSC стоял намертво, а джоб молча крутился без счётчика
попыток.
"""

import sys
from pathlib import Path

import pytest
from web3.exceptions import TransactionNotFound

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.blockchain.evm_adapter import EvmAdapter  # noqa: E402


class _Forbidden(Exception):
    """То, что публичный узел кидает на запрещённый метод."""


class _Eth:
    def __init__(self, receipt=None, error=None):
        self._receipt = receipt
        self._error = error
        self.calls = 0

    async def get_transaction_receipt(self, tx_hash):
        self.calls += 1
        if self._error:
            raise self._error
        if self._receipt is None:
            raise TransactionNotFound(tx_hash)
        return self._receipt


class _Web3:
    def __init__(self, receipt=None, error=None):
        self.eth = _Eth(receipt, error)


class _Manager:
    """Ротация: перебирает endpoint'ы, отказ = переход к следующему."""

    def __init__(self, endpoints):
        self.endpoints = endpoints
        self.failures = []

    async def execute(self, operation, timeout=None):
        last = None
        for w3 in self.endpoints:
            try:
                return await operation(w3)
            except Exception as exc:
                self.failures.append(w3)
                last = exc
        raise RuntimeError(f"all endpoints failed: {last}")


def _adapter(monkeypatch, manager, primary):
    monkeypatch.setattr(EvmAdapter, "__init__", lambda self: None)
    adapter = EvmAdapter()
    adapter.chain = "bsc"
    adapter._rpc_manager = manager
    adapter._use_rpc_manager = manager is not None
    adapter.w3 = primary
    return adapter


@pytest.mark.asyncio
async def test_receipt_fails_over_past_a_forbidden_endpoint(monkeypatch):
    """Первый endpoint запрещает метод - receipt берётся со второго."""
    dead = _Web3(error=_Forbidden("403 Forbidden"))
    alive = _Web3(receipt={"status": 1, "blockNumber": 42})
    manager = _Manager([dead, alive])

    adapter = _adapter(monkeypatch, manager, primary=dead)
    receipt = await adapter.get_transaction_receipt("0xabc")

    assert receipt == {"status": 1, "blockNumber": 42}
    assert manager.failures == [dead]
    assert alive.eth.calls == 1


@pytest.mark.asyncio
async def test_unmined_tx_does_not_burn_endpoints(monkeypatch):
    """
    «Ещё не в блоке» - ответ, а не отказ узла.

    Если пустить TransactionNotFound в ротацию, она пометит здоровые endpoint'ы
    сломанными и будет перебирать сеть на каждой проверке ожидающей транзакции.
    """
    first = _Web3(receipt=None)
    second = _Web3(receipt=None)
    manager = _Manager([first, second])

    adapter = _adapter(monkeypatch, manager, primary=first)
    receipt = await adapter.get_transaction_receipt("0xabc")

    assert receipt is None
    assert manager.failures == []
    assert second.eth.calls == 0  # до второго дело не дошло


@pytest.mark.asyncio
async def test_without_manager_the_primary_still_answers(monkeypatch):
    """Без ротации поведение прежнее - один endpoint."""
    primary = _Web3(receipt={"status": 1})

    adapter = _adapter(monkeypatch, None, primary=primary)
    assert await adapter.get_transaction_receipt("0xabc") == {"status": 1}


@pytest.mark.asyncio
async def test_all_endpoints_forbidden_raises(monkeypatch):
    """Когда метод запрещён везде, ошибка обязана всплыть наверх."""
    dead = _Web3(error=_Forbidden("403"))
    manager = _Manager([dead, _Web3(error=_Forbidden("403"))])

    adapter = _adapter(monkeypatch, manager, primary=dead)
    with pytest.raises(RuntimeError):
        await adapter.get_transaction_receipt("0xabc")
