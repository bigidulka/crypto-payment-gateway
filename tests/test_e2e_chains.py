"""
E2E тесты платёжного потока для всех сетей.

Сценарий для каждой сети:
1. Создаём инвойс через API
2. Выбираем сеть для оплаты
3. Отправляем токены с funder кошелька на депозитный адрес
4. Ждём пока инвойс перейдёт в статус CONFIRMED
5. Ждём пока sweeper выведет средства на treasury
6. Проверяем что баланс депозитного адреса = 0

Запуск:
    pytest tests/test_e2e_chains.py -v -s --chain=base
    pytest tests/test_e2e_chains.py -v -s  # Все сети
"""

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Добавляем корень проекта в path при прямом запуске
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

# Import from TOML config
from src.blockchain.chains import (
    get_chain_config,
    get_evm_chains,
    ERC20_ABI,
)


def build_chain_configs() -> dict:
    """Build CHAIN_CONFIGS from TOML."""
    result = {}
    for chain_name in get_evm_chains():
        cfg = get_chain_config(chain_name)
        
        # Get RPC from env or first from config
        env_var = f"{chain_name.upper()}_RPC_URL"
        rpc_url = os.getenv(env_var, cfg.rpc_url)
        
        tokens = {}
        decimals = {}
        for symbol, token_cfg in cfg.tokens.items():
            tokens[symbol] = token_cfg.contract_address
            decimals[symbol] = token_cfg.decimals
        
        result[chain_name] = {
            "chain_id": cfg.chain_id,
            "rpc_url": rpc_url,
            "native_symbol": cfg.native_symbol,
            "tokens": tokens,
            "decimals": decimals,
        }
    return result


# Load chain configs from TOML
CHAIN_CONFIGS = build_chain_configs()

logger = logging.getLogger(__name__)

# Конфигурация тестов
TEST_AMOUNT = Decimal("0.10")  # 0.10 USDC/USDT для теста
MAX_WAIT_CONFIRMED = 300  # 5 минут на подтверждение
MAX_WAIT_SWEPT = 300  # 5 минут на sweep
POLL_INTERVAL = 5  # Интервал polling в секундах


@dataclass
class PaymentTestResult:
    """Результат теста платежа."""

    chain: str
    token: str
    invoice_id: str
    public_id: str
    deposit_address: str
    payment_tx_hash: str | None = None
    sweep_tx_hash: str | None = None
    final_status: str = ""
    final_balance: Decimal = Decimal("0")
    success: bool = False
    error: str | None = None
    duration_seconds: float = 0


class ChainPaymentTester:
    """Тестер платежей для конкретной сети."""

    def __init__(
        self,
        chain: str,
        token: str,
        api_base_url: str,
        merchant_api_key: str,
        funder_private_key: str,
        treasury_address: str,
    ):
        self.chain = chain
        self.token = token
        self.api_base_url = api_base_url
        self.merchant_api_key = merchant_api_key
        self.funder_private_key = funder_private_key
        self.treasury_address = treasury_address

        self.chain_config = CHAIN_CONFIGS[chain]
        self.w3 = Web3(
            Web3.HTTPProvider(
                self.chain_config["rpc_url"], request_kwargs={"timeout": 60}
            )
        )
        self.funder_account = Account.from_key(funder_private_key)
        self.token_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain_config["tokens"][token]),
            abi=ERC20_ABI,
        )
        self.token_decimals = self.chain_config["decimals"][token]

    async def run_test(self) -> PaymentTestResult:
        """Выполнить полный тест платежа."""
        start_time = time.time()
        result = PaymentTestResult(
            chain=self.chain,
            token=self.token,
            invoice_id="",
            public_id="",
            deposit_address="",
        )

        try:
            async with httpx.AsyncClient(
                base_url=self.api_base_url, timeout=30.0
            ) as client:
                # 1. Создаём инвойс
                logger.info(
                    f"[{self.chain}] Создаём инвойс на {TEST_AMOUNT} {self.token}"
                )
                invoice = await self._create_invoice(client)
                result.invoice_id = invoice["id"]
                result.public_id = invoice["public_id"]
                logger.info(f"[{self.chain}] Инвойс создан: {result.public_id}")

                # 2. Выбираем сеть для оплаты
                logger.info(f"[{self.chain}] Выбираем сеть {self.chain}")
                payment_info = await self._select_chain(client, result.public_id)
                result.deposit_address = payment_info["deposit_address"]
                logger.info(
                    f"[{self.chain}] Депозитный адрес: {result.deposit_address}"
                )

                # 3. Отправляем токены
                logger.info(f"[{self.chain}] Отправляем {TEST_AMOUNT} {self.token}")
                tx_hash = await self._send_payment(result.deposit_address)
                result.payment_tx_hash = tx_hash
                logger.info(f"[{self.chain}] Платёж отправлен: {tx_hash}")

                # 4. Ждём подтверждения инвойса
                logger.info(f"[{self.chain}] Ждём подтверждения...")
                await self._wait_for_status(
                    client, result.public_id, "confirmed", MAX_WAIT_CONFIRMED
                )
                logger.info(f"[{self.chain}] Инвойс подтверждён!")

                # 5. Ждём sweep
                logger.info(f"[{self.chain}] Ждём sweep...")
                await self._wait_for_sweep(result.deposit_address, MAX_WAIT_SWEPT)
                logger.info(f"[{self.chain}] Средства выведены!")

                # 6. Проверяем финальный баланс
                final_balance = await self._get_token_balance(result.deposit_address)
                result.final_balance = final_balance
                result.success = final_balance == Decimal("0")

                # Получаем финальный статус
                status = await self._get_status(client, result.public_id)
                result.final_status = status["status"]

                if result.success:
                    logger.info(f"[{self.chain}] ✅ Тест пройден!")
                else:
                    result.error = f"Остался баланс: {final_balance}"
                    logger.error(f"[{self.chain}] ❌ Остался баланс: {final_balance}")

        except Exception as e:
            result.error = str(e)
            logger.exception(f"[{self.chain}] ❌ Ошибка: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    async def _create_invoice(self, client: httpx.AsyncClient) -> dict:
        """Создать инвойс через API."""
        response = await client.post(
            "/v1/invoices",
            json={
                "amount": str(TEST_AMOUNT),
                "asset": self.token,
                "allowed_chains": [self.chain],
                "ttl_minutes": 30,
                "metadata": {
                    "test": True,
                    "chain": self.chain,
                    "test_id": str(uuid.uuid4()),
                },
            },
            headers={
                "Authorization": f"Bearer {self.merchant_api_key}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        response.raise_for_status()
        return response.json()

    async def _select_chain(self, client: httpx.AsyncClient, public_id: str) -> dict:
        """Выбрать сеть для оплаты."""
        response = await client.post(
            f"/pay/{public_id}/select",
            json={
                "chain": self.chain,
                "token": self.token,
            },
        )
        response.raise_for_status()
        return response.json()

    async def _get_status(self, client: httpx.AsyncClient, public_id: str) -> dict:
        """Получить статус инвойса."""
        response = await client.get(f"/pay/{public_id}/status")
        response.raise_for_status()
        return response.json()

    async def _send_payment(self, deposit_address: str) -> str:
        """Отправить токены на депозитный адрес."""
        raw_amount = int(TEST_AMOUNT * (10**self.token_decimals))

        # Проверяем баланс funder
        funder_balance = self.token_contract.functions.balanceOf(
            self.funder_account.address
        ).call()
        if funder_balance < raw_amount:
            raise ValueError(
                f"Недостаточно {self.token} на funder. "
                f"Нужно: {TEST_AMOUNT}, Есть: {Decimal(funder_balance) / 10**self.token_decimals}"
            )

        # Получаем gas параметры
        nonce = self.w3.eth.get_transaction_count(self.funder_account.address)
        gas_price = self.w3.eth.gas_price

        # Собираем транзакцию
        tx_data = self.token_contract.functions.transfer(
            Web3.to_checksum_address(deposit_address), raw_amount
        )
        gas_estimate = tx_data.estimate_gas({"from": self.funder_account.address})

        tx = tx_data.build_transaction(
            {
                "from": self.funder_account.address,
                "nonce": nonce,
                "gas": int(gas_estimate * 1.3),
                "gasPrice": int(gas_price * 1.5),
                "chainId": self.chain_config["chain_id"],
            }
        )

        # Подписываем и отправляем
        signed = self.w3.eth.account.sign_transaction(tx, self.funder_private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

        # Ждём receipt
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise ValueError(f"Транзакция платежа failed: {tx_hash.hex()}")

        return tx_hash.hex()

    async def _wait_for_status(
        self,
        client: httpx.AsyncClient,
        public_id: str,
        target_status: str,
        timeout: int,
    ):
        """Ждать определённого статуса инвойса."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            status = await self._get_status(client, public_id)
            current_status = status["status"].lower()

            logger.debug(f"[{self.chain}] Статус: {current_status}")

            if current_status == target_status:
                return

            if current_status in ("expired", "failed"):
                raise ValueError(f"Инвойс перешёл в статус: {current_status}")

            await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError(f"Таймаут ожидания статуса {target_status}")

    async def _get_token_balance(self, address: str) -> Decimal:
        """Получить баланс токенов на адресе."""
        raw_balance = self.token_contract.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()
        return Decimal(raw_balance) / (10**self.token_decimals)

    async def _wait_for_sweep(self, deposit_address: str, timeout: int):
        """Ждать пока sweep выведет все средства."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            balance = await self._get_token_balance(deposit_address)
            logger.debug(f"[{self.chain}] Баланс депозита: {balance}")

            if balance == Decimal("0"):
                return

            await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError("Таймаут ожидания sweep")


# ============ Тесты для каждой сети ============


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDC"])
async def test_payment_base(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на Base."""
    tester = ChainPaymentTester(
        chain="base",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест Base/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDC"])
async def test_payment_arbitrum(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на Arbitrum."""
    tester = ChainPaymentTester(
        chain="arbitrum",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест Arbitrum/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDT"])
async def test_payment_bsc(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на BSC."""
    tester = ChainPaymentTester(
        chain="bsc",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест BSC/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDC"])
async def test_payment_polygon(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на Polygon."""
    tester = ChainPaymentTester(
        chain="polygon",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест Polygon/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDC"])
async def test_payment_avax(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на Avalanche."""
    tester = ChainPaymentTester(
        chain="avax",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест Avalanche/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["USDC"])
async def test_payment_optimism(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
    token: str,
):
    """E2E тест платежа на Optimism."""
    tester = ChainPaymentTester(
        chain="optimism",
        token=token,
        api_base_url=api_base_url,
        merchant_api_key=merchant_api_key,
        funder_private_key=funder_private_key,
        treasury_address=treasury_address,
    )
    result = await tester.run_test()

    assert result.success, f"Тест Optimism/{token} не пройден: {result.error}"
    assert result.final_balance == Decimal(
        "0"
    ), f"Остался баланс: {result.final_balance}"


# ============ Тест всех сетей сразу ============


@pytest.mark.asyncio
async def test_all_chains_sequential(
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
    treasury_address: str,
):
    """Последовательный тест всех сетей."""
    chains_tokens = [
        ("base", "USDC"),
        ("arbitrum", "USDC"),
        ("bsc", "USDT"),
        ("polygon", "USDC"),
        ("avax", "USDC"),
        ("optimism", "USDC"),
    ]

    results = []
    for chain, token in chains_tokens:
        logger.info(f"\n{'='*50}")
        logger.info(f"Тестируем {chain.upper()} / {token}")
        logger.info(f"{'='*50}")

        tester = ChainPaymentTester(
            chain=chain,
            token=token,
            api_base_url=api_base_url,
            merchant_api_key=merchant_api_key,
            funder_private_key=funder_private_key,
            treasury_address=treasury_address,
        )
        result = await tester.run_test()
        results.append(result)

    # Выводим сводку
    print("\n" + "=" * 70)
    print("СВОДКА ТЕСТОВ")
    print("=" * 70)
    print(f"{'Сеть':<12} {'Токен':<6} {'Статус':<10} {'Время':<10} {'Ошибка'}")
    print("-" * 70)

    all_passed = True
    for r in results:
        status = "✅ OK" if r.success else "❌ FAIL"
        time_str = f"{r.duration_seconds:.1f}s"
        error_str = (
            r.error[:30] + "..." if r.error and len(r.error) > 30 else (r.error or "")
        )
        print(f"{r.chain:<12} {r.token:<6} {status:<10} {time_str:<10} {error_str}")
        if not r.success:
            all_passed = False

    print("=" * 70)

    assert all_passed, "Не все тесты пройдены"


# ============ CLI для ручного запуска ============


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Добавляем корень проекта в path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Получаем параметры
    chain = sys.argv[1] if len(sys.argv) > 1 else "base"
    token = sys.argv[2] if len(sys.argv) > 2 else "USDC"

    from src.core.config import get_settings

    settings = get_settings()

    async def main():
        tester = ChainPaymentTester(
            chain=chain,
            token=token,
            api_base_url="http://localhost:8123",
            merchant_api_key="0713d50377810b07229272820e0b57c2",
            funder_private_key=settings.funder_private_key,
            treasury_address=settings.treasury_address,
        )
        result = await tester.run_test()

        print("\n" + "=" * 50)
        print("РЕЗУЛЬТАТ ТЕСТА")
        print("=" * 50)
        print(f"Сеть: {result.chain}")
        print(f"Токен: {result.token}")
        print(f"Инвойс: {result.public_id}")
        print(f"Депозит: {result.deposit_address}")
        print(f"TX платежа: {result.payment_tx_hash}")
        print(f"Статус: {result.final_status}")
        print(f"Финальный баланс: {result.final_balance}")
        print(f"Время: {result.duration_seconds:.1f} сек")
        print(f"Результат: {'✅ ПРОЙДЕН' if result.success else '❌ НЕ ПРОЙДЕН'}")
        if result.error:
            print(f"Ошибка: {result.error}")
        print("=" * 50)

    asyncio.run(main())
