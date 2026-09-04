"""
E2E сценарии сканера на anvil: настоящая цепочка, фальшивые деньги.

Чего не хватало юнит-тестам: они скармливали сканеру готовые словари логов.
Здесь логи настоящие - их майнит anvil, их забирает настоящий resilient
fetcher через eth_getLogs, парсит настоящий _parse_transfer_log, балансы
читает настоящий EvmAdapter через balanceOf. Подделана только БД: сессии
оплаты и чекпоинты, как в юнит-тестах.

Именно поэтому тест может исполнять транзакции сам: сеть локальная, средства
поддельные, проиграть нечего. Прогон всех пяти сценариев + доплаты занимает
несколько секунд и не стоит ни цента газа.

Требует docker (anvil). Нет docker - тест молча пропускается.

    pytest tests/test_anvil_e2e_scenarios.py -v
"""

import json
import socket
import subprocess
import time
import urllib.request
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3

import src.workers.evm_log_poller as poller
from src.blockchain.resilient_fetcher import ResilientLogFetcher, RpcEndpointSpec


ANVIL_PORT = 18545
ANVIL_URL = f"http://127.0.0.1:{ANVIL_PORT}"
# Первый детерминированный аккаунт anvil: и платёжщик, и деплойер токенов.
PAYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

ROOT = Path(__file__).resolve().parent.parent
BYTECODE = "0x" + (ROOT / "tests" / "contracts" / "mock_erc20.hex").read_text().strip()
# Настоящий ABI из solc-артефакта, чтобы не дублировать сигнатуры руками.
ABI = json.loads((ROOT / "tests" / "contracts" / "mock_erc20.abi").read_text())

INVOICE_AMOUNT = Decimal("100")


def _fresh_address(w3) -> str:
    """Адрес без ключа: получать переводы можно, тратить не нужно."""
    return w3.eth.account.create().address

@pytest.fixture(scope="module")
def anvil():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker недоступен")

    name = "anvil-e2e-scenarios"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"127.0.0.1:{ANVIL_PORT}:8545",
            "ghcr.io/foundry-rs/foundry:latest",
            # Entrypoint образа - "/bin/sh -c", поэтому вся команда одной
            # строкой: отдельными аргументами флаги достались бы shell.
            # 0.0.0.0 внутри контейнера, иначе port-forward не достукивается.
            "anvil --silent --host 0.0.0.0",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"anvil не поднялся: {proc.stderr[:200]}")

    probe = urllib.request.Request(
        ANVIL_URL,
        data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
        headers={"Content-Type": "application/json"},
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=2) as r:
                if json.loads(r.read()).get("result"):
                    break
        except Exception:
            time.sleep(0.5)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail("anvil не ответил за 60с")

    yield ANVIL_URL
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def chain(anvil):
    """Развёрнутые USDT/USDC, платёжщик с запасом, web3-клиент."""
    w3 = Web3(Web3.HTTPProvider(anvil))
    payer = w3.eth.account.from_key(PAYER_KEY)

    # Нонс ведём локально: anvil майнит мгновенно, но повторный запрос
    # счётчика между отправкой и майнингом возвращает старое значение.
    state = SimpleNamespace(nonce=w3.eth.get_transaction_count(payer.address))

    def send(tx_builder, wait: bool = False):
        tx = tx_builder(
            {"from": payer.address, "nonce": state.nonce, "gas": 1_000_000}
        )
        signed = payer.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        state.nonce += 1
        if wait:
            # Автомайн anvil не строго синхронен с ответом на отправку:
            # сканер, запущенный сразу после, может прочитать head до блока
            # с переводом и честно просканировать окно без него.
            w3.eth.wait_for_transaction_receipt(h)

    def deploy(symbol: str) -> str:
        contract = w3.eth.contract(abi=ABI, bytecode=BYTECODE)
        tx = contract.constructor(symbol, symbol).build_transaction(
            {"from": payer.address, "nonce": state.nonce}
        )
        signed = payer.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        state.nonce += 1
        return w3.eth.wait_for_transaction_receipt(h).contractAddress

    usdt, usdc = deploy("USDT"), deploy("USDC")
    for token in (usdt, usdc):
        contract = w3.eth.contract(address=token, abi=ABI)
        send(lambda opts, c=contract: c.functions.mint(
            payer.address, 10**24
        ).build_transaction(opts))

    return SimpleNamespace(w3=w3, payer=payer, usdt=usdt, usdc=usdc, _send=send, _state=state)


def pay(chain, token: str, to: str, amount: Decimal):
    """Один настоящий перевод токена - майнится отдельным блоком."""
    contract = chain.w3.eth.contract(address=token, abi=ABI)
    chain._send(
        lambda opts: contract.functions.transfer(
            to, int(amount * Decimal(10**18))
        ).build_transaction(opts),
        wait=True,
    )


def _payment_session(deposit: str, token: str = "USDT"):
    return SimpleNamespace(
        token=token,
        invoice=SimpleNamespace(amount=INVOICE_AMOUNT, created_at=datetime.now(UTC)),
        deposit_address=SimpleNamespace(address=deposit),
        received_amount=None,
        mismatch_reason=None,
        mismatch_token=None,
    )


@pytest.fixture
def wire(chain, monkeypatch):
    """Подключить настоящий фетчер и адаптер к anvil, БД оставить фейковой."""

    def _wire(payment_session, deposit: str):
        from src.blockchain.evm_adapter import EvmAdapter

        adapter = EvmAdapter("bsc", rpc_url=chain.w3.provider.endpoint_uri)

        tokens = {
            "USDT": SimpleNamespace(symbol="USDT", contract_address=chain.usdt, decimals=18),
            "USDC": SimpleNamespace(symbol="USDC", contract_address=chain.usdc, decimals=18),
        }
        config = SimpleNamespace(
            scanner_provider="rpc",
            scan_window=500,
            reorg_buffer=0,
            block_time_sec=1,
            native_symbol="BNB",
            native_decimals=18,
            tokens=tokens,
            is_native_asset=lambda s: s.upper() == "BNB",
            get_asset_contract=lambda s: (
                poller.NATIVE_TOKEN_CONTRACT if s.upper() == "BNB" else tokens[s.upper()].contract_address
            ),
        )

        async def _active_addresses(db_session, ch):
            # Реальный слой БД хранит адреса lowercase; матчинг в поллере
            # сравнивает тоже lowercase. anvil отдаёт checksummed.
            return {deposit.lower(): payment_session}

        session = type("S", (), {"commits": 0, "rollbacks": 0})()

        @asynccontextmanager
        async def _session_context():
            yield session

        async def _checkpoint(db_session, ch, chain_adapter, earliest):
            # окно в три блока назад: переводы этого сценария уже намайнены
            return max(1, await chain_adapter.get_latest_block_number() - 3)

        async def _update_checkpoint(db_session, ch, block_number):
            return None
        fetcher = ResilientLogFetcher("bsc", [RpcEndpointSpec(url=ANVIL_URL)])

        monkeypatch.setattr(poller, "get_session_context", _session_context)
        monkeypatch.setattr(poller, "get_active_deposit_addresses", _active_addresses)
        monkeypatch.setattr(poller, "get_chain_config", lambda ch: config)
        monkeypatch.setattr(poller, "get_evm_adapter", lambda ch: adapter)
        monkeypatch.setattr(poller, "get_or_create_checkpoint", _checkpoint)
        monkeypatch.setattr(poller, "update_checkpoint", _update_checkpoint)
        monkeypatch.setattr(poller, "get_resilient_fetcher", lambda ch: fetcher)
        # _parse_transfer_log НЕ трогаем: настоящие логи anvil должен парсить
        # настоящий парсер - в этом смысл теста.

        return payment_session

    return _wire


# === сценарии ===


@pytest.mark.asyncio
async def test_exact_payment_detected_on_real_logs(chain, wire):
    deposit = _fresh_address(chain.w3)
    ps = wire(_payment_session(deposit), deposit)
    pay(chain, chain.usdt, deposit, INVOICE_AMOUNT)

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason is None
    assert ps.received_amount == INVOICE_AMOUNT


@pytest.mark.asyncio
async def test_overpayment_credited_at_received(chain, wire):
    deposit = _fresh_address(chain.w3)
    ps = wire(_payment_session(deposit), deposit)
    pay(chain, chain.usdt, deposit, Decimal("105"))

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason is None
    assert ps.received_amount == Decimal("105")


@pytest.mark.asyncio
async def test_small_underpayment_paid_within_tolerance(chain, wire):
    deposit = _fresh_address(chain.w3)
    ps = wire(_payment_session(deposit), deposit)
    pay(chain, chain.usdt, deposit, Decimal("99.5"))

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason is None
    assert ps.received_amount == Decimal("99.5")


@pytest.mark.asyncio
async def test_big_underpayment_flagged_then_closed_by_topup(chain, wire):
    deposit = _fresh_address(chain.w3)
    ps = wire(_payment_session(deposit), deposit)

    pay(chain, chain.usdt, deposit, Decimal("80"))
    await poller.poll_chain("bsc")
    assert ps.mismatch_reason == poller.MISMATCH_UNDERPAID
    assert ps.received_amount == Decimal("80")

    # доплата вторым переводом: смотрится баланс адреса, не отдельный лог
    pay(chain, chain.usdt, deposit, Decimal("20"))
    await poller.poll_chain("bsc")
    assert ps.mismatch_reason is None
    assert ps.received_amount == INVOICE_AMOUNT


@pytest.mark.asyncio
async def test_wrong_token_flagged(chain, wire):
    deposit = _fresh_address(chain.w3)
    ps = wire(_payment_session(deposit), deposit)
    pay(chain, chain.usdc, deposit, INVOICE_AMOUNT)

    await poller.poll_chain("bsc")

    assert ps.mismatch_reason == poller.MISMATCH_WRONG_TOKEN
    assert ps.received_amount == INVOICE_AMOUNT
