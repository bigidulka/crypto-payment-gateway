"""Legacy treasury withdraw distinguishes definitive rejection from an unknown send."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from src.api.admin.router import withdraw_from_treasury
from src.api.admin.schemas import WithdrawRequest, WithdrawResponse
from src.blockchain import evm_adapter

KEY = "0x" + "11" * 32
TO = "0x" + "22" * 20


class FakeAdapter:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def _send(self, **_):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    send_native_transfer = _send
    send_erc20_transfer = _send


def _settings(key=KEY):
    return SimpleNamespace(funder_private_key=SecretStr(key))


async def _withdraw(monkeypatch, adapter, *, chain="base", token="USDC", amount="1.5", key=KEY):
    monkeypatch.setattr(evm_adapter, "get_evm_adapter", lambda _chain: adapter)
    request = WithdrawRequest(chain=chain, token=token, to_address=TO, amount=amount)
    return await withdraw_from_treasury(None, request, _settings(key))


def test_outcome_defaults_to_unknown_for_backward_compatible_construction():
    response = WithdrawResponse(success=False, chain="base", token="USDC", amount="1", to_address=TO)
    assert response.outcome == "unknown"


@pytest.mark.parametrize(
    ("chain", "token", "amount", "key"),
    [
        ("base", "USDC", "-1", KEY),
        ("base", "USDC", "abc", KEY),
        ("nochain", "USDC", "1", KEY),
        ("solana", "USDT", "1", KEY),
        ("ton", "USDT", "1", KEY),
        ("base", "USDC", "1", ""),
        ("base", "NOPE", "1", KEY),
    ],
)
async def test_pre_send_refusals_are_rejected_without_send(monkeypatch, chain, token, amount, key):
    adapter = FakeAdapter(result="0xabc")
    response = await _withdraw(monkeypatch, adapter, chain=chain, token=token, amount=amount, key=key)
    assert response.success is False
    assert response.outcome == "rejected"
    assert response.tx_hash is None
    assert adapter.calls == 0


@pytest.mark.parametrize("token", ["USDC", "NATIVE"])
async def test_broadcast_hash_is_sent(monkeypatch, token):
    adapter = FakeAdapter(result="0x" + "ab" * 32)
    response = await _withdraw(monkeypatch, adapter, token=token)
    assert response.success is True
    assert response.outcome == "sent"
    assert response.tx_hash == "0x" + "ab" * 32
    assert adapter.calls == 1


@pytest.mark.parametrize("token", ["USDC", "NATIVE"])
async def test_missing_hash_after_send_attempt_is_unknown(monkeypatch, token):
    adapter = FakeAdapter(result=None)
    response = await _withdraw(monkeypatch, adapter, token=token)
    assert response.success is False
    assert response.outcome == "unknown"
    assert adapter.calls == 1


async def test_exception_during_send_is_unknown(monkeypatch):
    adapter = FakeAdapter(error=TimeoutError("rpc timed out"))
    response = await _withdraw(monkeypatch, adapter)
    assert response.success is False
    assert response.outcome == "unknown"
    assert response.message.startswith("Error:")
