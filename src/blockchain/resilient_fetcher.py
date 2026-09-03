"""
Resilient Log Fetcher - высокоотказоустойчивый механизм получения логов.

Стратегия (в порядке приоритета):
1. Primary RPC + OR Topics (1 запрос на все адреса) - самый быстрый
2. Secondary RPC + OR Topics (failover) - если primary упал
3. Primary RPC + Parallel Batching - если OR Topics не работает
4. Secondary RPC + Parallel Batching - последний fallback

Особенности:
- Circuit breaker для каждого метода на каждом RPC
- Adaptive timeouts на основе latency
- Parallel execution для batching (до 10 параллельных запросов)
- Автоматическое определение поддержки OR Topics
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from web3 import AsyncWeb3
from web3.exceptions import Web3RPCError

from src.blockchain.chains import get_chain_config, get_transfer_event_signature

logger = logging.getLogger(__name__)


class FetchMethod(Enum):
    """Методы получения логов."""

    OR_TOPICS = "or_topics"  # Один запрос с OR в topics
    PARALLEL_BATCH = "parallel_batch"  # Параллельные запросы
    SEQUENTIAL = "sequential"  # Последовательные запросы (fallback)


class CircuitState(Enum):
    """Состояние circuit breaker."""

    CLOSED = "closed"  # Нормальная работа
    OPEN = "open"  # Отказ - не пропускаем запросы
    HALF_OPEN = "half_open"  # Пробуем один запрос


@dataclass
class CircuitBreaker:
    """Circuit breaker для метода/RPC комбинации."""

    failure_threshold: int = 3  # После скольких ошибок открыть
    recovery_timeout: float = 60.0  # Через сколько секунд пробовать снова
    half_open_max_calls: int = 1  # Сколько тестовых вызовов

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0

    def can_execute(self) -> bool:
        """Можно ли выполнить запрос."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Проверяем не пора ли попробовать снова
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info("Circuit breaker transitioning to HALF_OPEN")
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_calls < self.half_open_max_calls

        return False

    def record_success(self) -> None:
        """Записать успешный вызов."""
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info("Circuit breaker recovered to CLOSED")
        self.failure_count = 0

    def record_failure(self) -> None:
        """Записать неудачный вызов."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning("Circuit breaker back to OPEN after half-open failure")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker OPENED after {self.failure_count} failures"
            )


@dataclass(frozen=True)
class RpcEndpointSpec:
    """Описание одного RPC endpoint до создания web3."""

    url: str
    priority: int = 1  # 1 = primary, 2 = secondary
    headers: dict[str, str] = field(default_factory=dict)

    def build_web3(self, timeout: float) -> AsyncWeb3:
        """Создать web3 клиент, пробросив заголовки авторизации."""
        request_kwargs: dict[str, Any] = {"timeout": timeout}
        if self.headers:
            request_kwargs["headers"] = dict(self.headers)
        return AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.url, request_kwargs=request_kwargs))


@dataclass
class RpcEndpointState:
    """Состояние RPC endpoint для log fetcher."""

    url: str
    web3: AsyncWeb3
    priority: int = 1  # 1 = primary, 2 = secondary

    # Поддержка OR Topics (определяется динамически)
    supports_or_topics: bool | None = None  # None = не проверено
    or_topics_tested: bool = False

    # Circuit breakers для каждого метода
    circuit_or_topics: CircuitBreaker = field(default_factory=CircuitBreaker)
    circuit_parallel: CircuitBreaker = field(default_factory=CircuitBreaker)

    # Статистика
    avg_latency_ms: float = 0.0
    total_requests: int = 0
    failed_requests: int = 0

    def update_latency(self, latency_ms: float) -> None:
        """Обновить среднюю latency (EMA)."""
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = 0.7 * self.avg_latency_ms + 0.3 * latency_ms


@dataclass
class TransferLogResult:
    """Результат получения логов."""

    logs: list[dict]
    method_used: FetchMethod
    rpc_used: str
    latency_ms: float
    from_block: int
    to_block: int
    is_complete: bool
    failed_address_count: int


def _hex_str(value: Any) -> str:
    """Привести hash из web3 к строке с 0x-префиксом."""
    text = value.hex() if isinstance(value, bytes) else str(value)
    return text if text.startswith("0x") else f"0x{text}"


@dataclass(frozen=True)
class NativeTransfer:
    """Входящий перевод нативной монеты, найденный по балансу адреса."""

    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    value_wei: int


@dataclass
class NativeScanResult:
    """Результат поиска нативных депозитов."""

    transfers: list[NativeTransfer]
    rpc_used: str
    latency_ms: float
    is_complete: bool
    failed_address_count: int


class ResilientLogFetcher:
    """
    Высокоотказоустойчивый fetcher для Transfer логов.

    Использует комбинацию методов и RPC для максимальной надёжности.
    """

    # Лимиты
    MAX_PARALLEL_REQUESTS = 10  # Максимум параллельных запросов
    BATCH_SIZE = 50  # Адресов на batch при parallel fetching
    MAX_FAILED_ADDRESS_RATIO = 0.05  # Partial result tolerance before retrying next RPC
    MAX_FAILED_ADDRESSES_TOLERATED = 5  # Low partial loss accepted to keep checkpoints moving
    OR_TOPICS_LIMIT = 100  # Максимум адресов для OR Topics

    # Timeouts
    DEFAULT_TIMEOUT = 30.0
    PARALLEL_TIMEOUT = 15.0  # Меньше таймаут для параллельных

    def __init__(self, chain: str, rpc_endpoints: list[RpcEndpointSpec]):
        """
        Args:
            chain: Имя сети
            rpc_endpoints: Список endpoint spec - priority 1 = primary
        """
        self.chain = chain
        self.config = get_chain_config(chain)

        # Инициализируем endpoints
        self.endpoints: list[RpcEndpointState] = []
        for spec in sorted(rpc_endpoints, key=lambda item: item.priority):
            self.endpoints.append(
                RpcEndpointState(
                    url=spec.url,
                    web3=spec.build_web3(self.DEFAULT_TIMEOUT),
                    priority=spec.priority,
                )
            )

        if not self.endpoints:
            raise ValueError(f"No RPC endpoints for {chain}")

        logger.info(
            f"[{chain}] ResilientLogFetcher initialized with {len(self.endpoints)} endpoints"
        )

    def _get_primary(self) -> RpcEndpointState | None:
        """Получить primary endpoint."""
        for ep in self.endpoints:
            if ep.priority == 1:
                return ep
        return self.endpoints[0] if self.endpoints else None

    def _get_secondary(self) -> RpcEndpointState | None:
        """Получить secondary endpoint."""
        for ep in self.endpoints:
            if ep.priority == 2:
                return ep
        return None

    async def _test_or_topics_support(self, endpoint: RpcEndpointState) -> bool:
        """Проверить поддерживает ли RPC OR в topics."""
        if endpoint.or_topics_tested:
            return endpoint.supports_or_topics or False

        try:
            # Тестовый запрос с OR topics
            latest = await endpoint.web3.eth.block_number
            test_block = latest - 100

            # Два тестовых адреса
            test_addresses = [
                "0x" + "00" * 19 + "01".zfill(2),
                "0x" + "00" * 19 + "02".zfill(2),
            ]
            padded = ["0x" + addr[2:].zfill(64) for addr in test_addresses]

            await endpoint.web3.eth.get_logs(
                {
                    "fromBlock": test_block,
                    "toBlock": test_block + 10,
                    "topics": [
                        get_transfer_event_signature(),
                        None,
                        padded,  # OR список
                    ],
                }
            )

            endpoint.supports_or_topics = True
            endpoint.or_topics_tested = True
            logger.info(f"[{self.chain}] {endpoint.url} supports OR topics ✓")
            return True

        except Exception as e:
            error_msg = str(e).lower()
            if (
                "invalid" in error_msg
                or "not supported" in error_msg
                or "array" in error_msg
            ):
                endpoint.supports_or_topics = False
                logger.info(f"[{self.chain}] {endpoint.url} does NOT support OR topics")
            else:
                # Другая ошибка - не знаем точно
                endpoint.supports_or_topics = True  # Оптимистично
                logger.warning(f"[{self.chain}] OR topics test inconclusive: {e}")

            endpoint.or_topics_tested = True
            return endpoint.supports_or_topics or False

    async def _fetch_with_or_topics(
        self,
        endpoint: RpcEndpointState,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[dict]:
        """
        Получить логи одним запросом с OR в topics.
        """
        if not endpoint.circuit_or_topics.can_execute():
            raise RuntimeError("Circuit breaker open for OR topics")

        # Паддинг адресов
        padded_addresses = ["0x" + addr[2:].lower().zfill(64) for addr in to_addresses]

        try:
            start = time.time()

            logs = await asyncio.wait_for(
                endpoint.web3.eth.get_logs(
                    {
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "address": token_contracts,
                        "topics": [
                            get_transfer_event_signature(),
                            None,  # from - любой
                            padded_addresses,  # to - OR список
                        ],
                    }
                ),
                timeout=timeout,
            )

            latency = (time.time() - start) * 1000
            endpoint.update_latency(latency)
            endpoint.total_requests += 1
            endpoint.circuit_or_topics.record_success()

            return list(logs)

        except Exception as e:
            endpoint.failed_requests += 1
            endpoint.circuit_or_topics.record_failure()
            raise

    async def _fetch_single_address(
        self,
        endpoint: RpcEndpointState,
        from_block: int,
        to_block: int,
        to_address: str,
        token_contracts: list[str],
        timeout: float = PARALLEL_TIMEOUT,
    ) -> list[dict]:
        """Получить логи для одного адреса."""
        padded = "0x" + to_address[2:].lower().zfill(64)

        logs = await asyncio.wait_for(
            endpoint.web3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": token_contracts,
                    "topics": [
                        get_transfer_event_signature(),
                        None,
                        padded,
                    ],
                }
            ),
            timeout=timeout,
        )

        return list(logs)

    async def _fetch_parallel_batch(
        self,
        endpoint: RpcEndpointState,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
    ) -> tuple[list[dict], int]:
        """
        Получить логи параллельными запросами.

        Returns:
            tuple: (logs, failed_address_count)
        """
        if not endpoint.circuit_parallel.can_execute():
            raise RuntimeError("Circuit breaker open for parallel fetch")

        start = time.time()
        all_logs: list[dict] = []
        errors: list[Exception] = []

        # Создаём семафор для ограничения параллельности
        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_REQUESTS)

        async def fetch_with_semaphore(address: str) -> list[dict]:
            async with semaphore:
                try:
                    return await self._fetch_single_address(
                        endpoint, from_block, to_block, address, token_contracts
                    )
                except Exception as e:
                    errors.append(e)
                    return []

        # Запускаем все параллельно
        tasks = [fetch_with_semaphore(addr) for addr in to_addresses]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        for result in results:
            all_logs.extend(result)

        latency = (time.time() - start) * 1000
        endpoint.update_latency(latency)
        endpoint.total_requests += 1

        # Если есть существенные ошибки - считаем endpoint неуспешным и пробуем следующий.
        # Нельзя возвращать partial result: poller не двигает checkpoint, и сеть застревает.
        error_rate = len(errors) / len(to_addresses) if to_addresses else 0
        if (
            len(errors) > self.MAX_FAILED_ADDRESSES_TOLERATED
            and error_rate >= self.MAX_FAILED_ADDRESS_RATIO
        ):
            endpoint.circuit_parallel.record_failure()
            raise RuntimeError(
                f"Too many errors in parallel fetch: {len(errors)}/{len(to_addresses)}"
            )

        endpoint.circuit_parallel.record_success()
        return all_logs, len(errors)

    async def fetch_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str],
    ) -> TransferLogResult:
        """
        Получить Transfer логи с максимальной отказоустойчивостью.

        Стратегия:
        1. Primary + OR Topics
        2. Secondary + OR Topics
        3. Primary + Parallel Batch
        4. Secondary + Parallel Batch
        5. Primary + Sequential (последний fallback)
        """
        if not to_addresses:
            return TransferLogResult(
                logs=[],
                method_used=FetchMethod.OR_TOPICS,
                rpc_used="none",
                latency_ms=0,
                from_block=from_block,
                to_block=to_block,
                is_complete=True,
                failed_address_count=0,
            )

        primary = self._get_primary()
        secondary = self._get_secondary()

        last_error: Exception | None = None

        # === PHASE 1: Try OR Topics ===

        if len(to_addresses) <= self.OR_TOPICS_LIMIT:
            # 1a. Primary + OR Topics
            if primary and primary.circuit_or_topics.can_execute():
                # Проверяем поддержку OR Topics если не проверяли
                if not primary.or_topics_tested:
                    await self._test_or_topics_support(primary)

                if primary.supports_or_topics:
                    try:
                        start = time.time()
                        logs = await self._fetch_with_or_topics(
                            primary, from_block, to_block, to_addresses, token_contracts
                        )
                        latency = (time.time() - start) * 1000

                        logger.debug(
                            f"[{self.chain}] OR Topics success (primary): "
                            f"{len(logs)} logs in {latency:.0f}ms"
                        )

                        return TransferLogResult(
                            logs=logs,
                            method_used=FetchMethod.OR_TOPICS,
                            rpc_used=primary.url,
                            latency_ms=latency,
                            from_block=from_block,
                            to_block=to_block,
                            is_complete=True,
                            failed_address_count=0,
                        )
                    except Exception as e:
                        last_error = e
                        logger.warning(f"[{self.chain}] Primary OR Topics failed: {e}")

            # 1b. Secondary + OR Topics
            if secondary and secondary.circuit_or_topics.can_execute():
                if not secondary.or_topics_tested:
                    await self._test_or_topics_support(secondary)

                if secondary.supports_or_topics:
                    try:
                        start = time.time()
                        logs = await self._fetch_with_or_topics(
                            secondary,
                            from_block,
                            to_block,
                            to_addresses,
                            token_contracts,
                        )
                        latency = (time.time() - start) * 1000

                        logger.debug(
                            f"[{self.chain}] OR Topics success (secondary): "
                            f"{len(logs)} logs in {latency:.0f}ms"
                        )

                        return TransferLogResult(
                            logs=logs,
                            method_used=FetchMethod.OR_TOPICS,
                            rpc_used=secondary.url,
                            latency_ms=latency,
                            from_block=from_block,
                            to_block=to_block,
                            is_complete=True,
                            failed_address_count=0,
                        )
                    except Exception as e:
                        last_error = e
                        logger.warning(
                            f"[{self.chain}] Secondary OR Topics failed: {e}"
                        )

        # === PHASE 2: Parallel Batch ===

        # 2a. Primary + Parallel
        if primary and primary.circuit_parallel.can_execute():
            try:
                start = time.time()
                logs, failed_address_count = await self._fetch_parallel_batch(
                    primary, from_block, to_block, to_addresses, token_contracts
                )
                latency = (time.time() - start) * 1000

                logger.debug(
                    f"[{self.chain}] Parallel batch success (primary): "
                    f"{len(logs)} logs in {latency:.0f}ms"
                )

                return TransferLogResult(
                    logs=logs,
                    method_used=FetchMethod.PARALLEL_BATCH,
                    rpc_used=primary.url,
                    latency_ms=latency,
                    from_block=from_block,
                    to_block=to_block,
                    is_complete=(failed_address_count == 0),
                    failed_address_count=failed_address_count,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"[{self.chain}] Primary parallel batch failed: {e}")

        # 2b. Secondary + Parallel
        if secondary and secondary.circuit_parallel.can_execute():
            try:
                start = time.time()
                logs, failed_address_count = await self._fetch_parallel_batch(
                    secondary, from_block, to_block, to_addresses, token_contracts
                )
                latency = (time.time() - start) * 1000

                logger.debug(
                    f"[{self.chain}] Parallel batch success (secondary): "
                    f"{len(logs)} logs in {latency:.0f}ms"
                )

                return TransferLogResult(
                    logs=logs,
                    method_used=FetchMethod.PARALLEL_BATCH,
                    rpc_used=secondary.url,
                    latency_ms=latency,
                    from_block=from_block,
                    to_block=to_block,
                    is_complete=(failed_address_count == 0),
                    failed_address_count=failed_address_count,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"[{self.chain}] Secondary parallel batch failed: {e}")

        # === PHASE 3: Sequential Fallback ===
        # Используем любой endpoint, игнорируем circuit breakers

        for endpoint in self.endpoints:
            try:
                start = time.time()
                logs: list[dict] = []

                failed_address_count = 0

                for addr in to_addresses:
                    try:
                        addr_logs = await self._fetch_single_address(
                            endpoint,
                            from_block,
                            to_block,
                            addr,
                            token_contracts,
                            timeout=self.DEFAULT_TIMEOUT,
                        )
                        logs.extend(addr_logs)
                    except Exception:
                        failed_address_count += 1
                        continue  # Пропускаем ошибки отдельных адресов

                latency = (time.time() - start) * 1000

                logger.info(
                    f"[{self.chain}] Sequential fallback: {len(logs)} logs in {latency:.0f}ms"
                )

                return TransferLogResult(
                    logs=logs,
                    method_used=FetchMethod.SEQUENTIAL,
                    rpc_used=endpoint.url,
                    latency_ms=latency,
                    from_block=from_block,
                    to_block=to_block,
                    is_complete=(failed_address_count == 0),
                    failed_address_count=failed_address_count,
                )
            except Exception as e:
                last_error = e
                continue

        # Все методы провалились
        logger.error(f"[{self.chain}] All fetch methods failed!")
        raise RuntimeError(f"All RPC methods failed. Last error: {last_error}")

    async def _native_balance(
        self, endpoint: RpcEndpointState, address: str, block: int
    ) -> int:
        """Баланс нативной монеты на конкретной высоте."""
        return await endpoint.web3.eth.get_balance(
            endpoint.web3.to_checksum_address(address),
            block_identifier=block,
        )

    async def _find_native_transfers_for_address(
        self,
        endpoint: RpcEndpointState,
        address: str,
        from_block: int,
        to_block: int,
    ) -> list[NativeTransfer]:
        """
        Найти входящие нативные переводы на адрес в диапазоне блоков.

        Логов у нативных переводов нет, поэтому баланс на границах диапазона
        сравнивается напрямую, а точный блок поступления находится бинарным
        поиском. Так стоимость не зависит от ширины окна: ~log2(window)
        запросов на адрес вместо полного обхода каждого блока.
        """
        baseline_block = max(from_block - 1, 0)
        baseline = await self._native_balance(endpoint, address, baseline_block)
        latest = await self._native_balance(endpoint, address, to_block)
        if latest <= baseline:
            return []

        transfers: list[NativeTransfer] = []
        checksum = endpoint.web3.to_checksum_address(address)
        cursor_block = baseline_block
        cursor_balance = baseline

        while cursor_balance < latest and cursor_block < to_block:
            # Наименьший блок, где баланс уже больше cursor_balance.
            low, high = cursor_block + 1, to_block
            while low < high:
                mid = (low + high) // 2
                if await self._native_balance(endpoint, address, mid) > cursor_balance:
                    high = mid
                else:
                    low = mid + 1

            block = await endpoint.web3.eth.get_block(low, full_transactions=True)
            found_in_block = False
            for tx in block.get("transactions", []):
                if not isinstance(tx, dict):
                    continue
                to_field = tx.get("to")
                value = int(tx.get("value", 0) or 0)
                if not to_field or value <= 0:
                    continue
                if endpoint.web3.to_checksum_address(to_field) != checksum:
                    continue
                transfers.append(
                    NativeTransfer(
                        tx_hash=_hex_str(tx.get("hash", "")),
                        block_number=low,
                        from_address=str(tx.get("from", "")).lower(),
                        to_address=address.lower(),
                        value_wei=value,
                    )
                )
                found_in_block = True

            cursor_block = low
            cursor_balance = await self._native_balance(endpoint, address, low)
            if not found_in_block:
                # Баланс вырос без внешней транзакции (internal call, coinbase).
                # Двигаем курсор, чтобы не зациклиться.
                logger.debug(
                    f"[{self.chain}] Native balance of {address} changed in block "
                    f"{low} without a matching top-level transaction"
                )

        return transfers

    async def fetch_native_transfers(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
    ) -> NativeScanResult:
        """
        Найти входящие нативные депозиты для списка адресов.

        Args:
            from_block: Первый блок диапазона (включительно)
            to_block: Последний блок диапазона (включительно)
            to_addresses: Адреса получателей

        Returns:
            NativeScanResult; is_complete=False если часть адресов не проверена
        """
        started = time.monotonic()
        if not to_addresses:
            return NativeScanResult(
                transfers=[],
                rpc_used="",
                latency_ms=0.0,
                is_complete=True,
                failed_address_count=0,
            )

        last_error: Exception | None = None
        for endpoint in self.endpoints:
            transfers: list[NativeTransfer] = []
            failed = 0
            for address in to_addresses:
                try:
                    transfers.extend(
                        await self._find_native_transfers_for_address(
                            endpoint, address, from_block, to_block
                        )
                    )
                except Exception as e:
                    failed += 1
                    last_error = e
                    logger.warning(
                        f"[{self.chain}] Native scan failed for {address} "
                        f"on {endpoint.url}: {e}"
                    )

            if failed < len(to_addresses):
                return NativeScanResult(
                    transfers=transfers,
                    rpc_used=endpoint.url,
                    latency_ms=(time.monotonic() - started) * 1000,
                    is_complete=failed == 0,
                    failed_address_count=failed,
                )

        logger.error(
            f"[{self.chain}] Native scan failed on every endpoint: {last_error}"
        )
        return NativeScanResult(
            transfers=[],
            rpc_used="",
            latency_ms=(time.monotonic() - started) * 1000,
            is_complete=False,
            failed_address_count=len(to_addresses),
        )

    def get_stats(self) -> dict:
        """Получить статистику по endpoints."""
        return {
            "chain": self.chain,
            "endpoints": [
                {
                    "url": ep.url[:50] + "...",
                    "priority": ep.priority,
                    "supports_or_topics": ep.supports_or_topics,
                    "avg_latency_ms": round(ep.avg_latency_ms, 1),
                    "total_requests": ep.total_requests,
                    "failed_requests": ep.failed_requests,
                    "circuit_or_topics": ep.circuit_or_topics.state.value,
                    "circuit_parallel": ep.circuit_parallel.state.value,
                }
                for ep in self.endpoints
            ],
        }


# === Global Instances ===

_fetchers: dict[str, ResilientLogFetcher] = {}


def get_resilient_fetcher(chain: str) -> ResilientLogFetcher | None:
    """Получить fetcher для сети."""
    return _fetchers.get(chain)


async def init_resilient_fetchers(
    rpc_config: dict[str, list[RpcEndpointSpec]],
) -> None:
    """
    Инициализировать fetchers для всех сетей.

    Args:
        rpc_config: {chain: [primary_spec, secondary_spec, ...]} в порядке приоритета
    """
    global _fetchers

    for chain, specs in rpc_config.items():
        if not specs:
            continue

        _fetchers[chain] = ResilientLogFetcher(chain, specs)
        logger.info(
            f"[{chain}] ResilientLogFetcher initialized with {len(specs)} endpoints"
        )


def build_endpoint_specs(
    urls: list[str],
    *,
    keyed_url: str = "",
    keyed_headers: dict[str, str] | None = None,
) -> list[RpcEndpointSpec]:
    """
    Собрать список endpoint spec: keyed scanner RPC первым, публичные следом.

    Args:
        urls: Публичные RPC URLs из chains.toml, в порядке приоритета
        keyed_url: URL keyed scanner RPC (пусто = не используется)
        keyed_headers: Заголовки авторизации для keyed_url
    """
    specs: list[RpcEndpointSpec] = []
    if keyed_url:
        specs.append(
            RpcEndpointSpec(
                url=keyed_url,
                priority=1,
                headers=dict(keyed_headers or {}),
            )
        )

    offset = len(specs)
    for index, url in enumerate(urls):
        specs.append(RpcEndpointSpec(url=url, priority=offset + index + 1))
    return specs


async def init_scanner_fetchers(chains: list[str]) -> int:
    """
    Инициализировать fetchers для сканера депозитов.

    Keyed scanner RPC (если настроен) становится primary, публичные RPC из
    chains.toml идут следом как failover.

    Args:
        chains: Сети, для которых нужен fetcher

    Returns:
        Количество сетей, для которых fetcher создан
    """
    from src.core.config import get_settings

    settings = get_settings()
    keyed_headers = settings.get_scanner_rpc_headers()

    rpc_config: dict[str, list[RpcEndpointSpec]] = {}
    for chain in chains:
        specs = build_endpoint_specs(
            settings.get_rpc_urls(chain),
            keyed_url=settings.get_scanner_rpc_url(chain),
            keyed_headers=keyed_headers,
        )
        if specs:
            rpc_config[chain] = specs

    if rpc_config:
        await init_resilient_fetchers(rpc_config)
    return len(rpc_config)


async def close_resilient_fetchers() -> None:
    """Закрыть все fetchers и их HTTP-сессии."""
    global _fetchers

    for fetcher in _fetchers.values():
        for endpoint in fetcher.endpoints:
            disconnect = getattr(endpoint.web3.provider, "disconnect", None)
            if disconnect is None:
                continue
            try:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"Failed to close RPC session {endpoint.url}: {e}")

    _fetchers = {}
