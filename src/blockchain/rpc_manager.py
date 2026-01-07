"""
Multi-RPC Manager с ротацией, failover и health checks.

Обеспечивает:
- Автоматическую ротацию между RPC endpoints
- Failover при ошибках
- Health checks и исключение нездоровых endpoints
- Latency-based выбор
- Rate limit awareness
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

from web3 import AsyncWeb3
from web3.exceptions import Web3RPCError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RpcHealth(Enum):
    """Состояние здоровья RPC endpoint."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Высокая latency или частичные ошибки
    UNHEALTHY = "unhealthy"  # Не отвечает или постоянные ошибки
    RATE_LIMITED = "rate_limited"  # Превышен лимит запросов


class RotationStrategy(Enum):
    """Стратегия ротации RPC."""

    ROUND_ROBIN = "round_robin"  # Последовательно
    RANDOM = "random"  # Случайный выбор
    FAILOVER = "failover"  # Первый доступный по приоритету
    LATENCY = "latency"  # Самый быстрый
    WEIGHTED = "weighted"  # По весам
    HYBRID = "hybrid"  # Latency среди healthy + failover на degraded


@dataclass
class RpcEndpoint:
    """Конфигурация одного RPC endpoint."""

    url: str
    priority: int = 1  # 1 = высший приоритет
    weight: int = 1  # Для weighted стратегии
    max_requests_per_second: int = 100  # Rate limit

    # Runtime состояние
    health: RpcHealth = RpcHealth.HEALTHY
    avg_latency_ms: float = 0.0
    total_requests: int = 0
    failed_requests: int = 0
    last_error: str | None = None
    last_error_time: float = 0.0
    last_success_time: float = 0.0
    consecutive_failures: int = 0
    rate_limit_until: float = 0.0  # Unix timestamp когда rate limit истечёт

    # Web3 instance (lazy init)
    _web3: AsyncWeb3 | None = field(default=None, repr=False)

    @property
    def web3(self) -> AsyncWeb3:
        """Lazy initialization of Web3 instance."""
        if self._web3 is None:
            self._web3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.url))
        return self._web3

    @property
    def is_available(self) -> bool:
        """Проверить доступен ли endpoint для запросов."""
        now = time.time()

        # Rate limited
        if self.rate_limit_until > now:
            return False

        # Unhealthy - ждём cooldown
        if self.health == RpcHealth.UNHEALTHY:
            # Cooldown: 30 секунд после последней ошибки
            if now - self.last_error_time < 30:
                return False

        return True

    @property
    def error_rate(self) -> float:
        """Процент ошибок."""
        if self.total_requests == 0:
            return 0.0
        return self.failed_requests / self.total_requests

    def record_success(self, latency_ms: float) -> None:
        """Записать успешный запрос."""
        self.total_requests += 1
        self.consecutive_failures = 0
        self.last_success_time = time.time()

        # Exponential moving average для latency
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = 0.8 * self.avg_latency_ms + 0.2 * latency_ms

        # Восстанавливаем health
        if self.health != RpcHealth.HEALTHY:
            self.health = RpcHealth.HEALTHY
            logger.info(f"RPC {self.url} recovered to HEALTHY")

    def record_failure(self, error: str) -> None:
        """Записать неудачный запрос."""
        self.total_requests += 1
        self.failed_requests += 1
        self.consecutive_failures += 1
        self.last_error = error
        self.last_error_time = time.time()

        # Rate limit detection
        if "rate limit" in error.lower() or "-32005" in error:
            self.health = RpcHealth.RATE_LIMITED
            self.rate_limit_until = time.time() + 60  # 1 минута cooldown
            logger.warning(f"RPC {self.url} rate limited, cooldown 60s")
        elif self.consecutive_failures >= 3:
            self.health = RpcHealth.UNHEALTHY
            logger.error(
                f"RPC {self.url} marked UNHEALTHY after {self.consecutive_failures} failures"
            )
        elif self.consecutive_failures >= 1:
            self.health = RpcHealth.DEGRADED


@dataclass
class RpcManagerConfig:
    """Конфигурация RPC Manager."""

    strategy: RotationStrategy = RotationStrategy.HYBRID  # Лучший для payment gateway
    max_retries: int = 3
    retry_delay_ms: int = 500
    health_check_interval_sec: int = 60
    request_timeout_sec: int = 30


class RpcManager:
    """
    Менеджер множества RPC endpoints для одной сети.

    Пример использования:
        manager = RpcManager("arbitrum", [
            RpcEndpoint("https://arb1.arbitrum.io/rpc", priority=1),
            RpcEndpoint("https://arb-mainnet.g.alchemy.com/v2/KEY", priority=2),
        ])

        # Получить Web3 instance
        web3 = await manager.get_web3()

        # Выполнить запрос с автоматическим failover
        block = await manager.execute(lambda w3: w3.eth.block_number)
    """

    def __init__(
        self,
        chain: str,
        endpoints: list[RpcEndpoint],
        config: RpcManagerConfig | None = None,
    ):
        self.chain = chain
        self.endpoints = endpoints
        self.config = config or RpcManagerConfig()
        self._current_index = 0
        self._lock = asyncio.Lock()

        if not endpoints:
            raise ValueError(f"No RPC endpoints configured for {chain}")

        # Сортируем по приоритету
        self.endpoints.sort(key=lambda e: e.priority)

        logger.info(
            f"RpcManager initialized for {chain} with {len(endpoints)} endpoints, "
            f"strategy={self.config.strategy.value}"
        )

    def _get_available_endpoints(self) -> list[RpcEndpoint]:
        """Получить список доступных endpoints."""
        return [ep for ep in self.endpoints if ep.is_available]

    def _select_endpoint(self) -> RpcEndpoint | None:
        """Выбрать endpoint по текущей стратегии."""
        available = self._get_available_endpoints()

        if not available:
            # Все endpoints недоступны - попробуем degraded
            degraded = [ep for ep in self.endpoints if ep.health != RpcHealth.UNHEALTHY]
            if degraded:
                logger.warning(
                    f"[{self.chain}] All healthy endpoints unavailable, using degraded"
                )
                available = degraded
            else:
                logger.error(f"[{self.chain}] No available RPC endpoints!")
                return None

        strategy = self.config.strategy

        if strategy == RotationStrategy.ROUND_ROBIN:
            self._current_index = (self._current_index + 1) % len(available)
            return available[self._current_index]

        elif strategy == RotationStrategy.RANDOM:
            return random.choice(available)

        elif strategy == RotationStrategy.FAILOVER:
            # Первый по приоритету
            return min(available, key=lambda ep: ep.priority)

        elif strategy == RotationStrategy.LATENCY:
            # Самый быстрый
            return min(available, key=lambda ep: ep.avg_latency_ms or float("inf"))

        elif strategy == RotationStrategy.WEIGHTED:
            # Weighted random
            total_weight = sum(ep.weight for ep in available)
            r = random.uniform(0, total_weight)
            cumulative = 0
            for ep in available:
                cumulative += ep.weight
                if r <= cumulative:
                    return ep
            return available[-1]

        elif strategy == RotationStrategy.HYBRID:
            # HYBRID: Latency среди healthy, failover на degraded
            # 1. Сначала пробуем healthy endpoints, выбираем по latency
            healthy = [ep for ep in available if ep.health == RpcHealth.HEALTHY]

            if healthy:
                # Выбираем самый быстрый среди healthy
                # Если latency ещё не замерен (0), даём шанс с низким приоритетом
                return min(
                    healthy,
                    key=lambda ep: (
                        (
                            ep.avg_latency_ms if ep.avg_latency_ms > 0 else 50.0
                        ),  # Default 50ms
                        ep.priority,  # При равном latency - по приоритету
                    ),
                )

            # 2. Нет healthy - failover на degraded по приоритету
            degraded = [ep for ep in available if ep.health == RpcHealth.DEGRADED]
            if degraded:
                logger.warning(
                    f"[{self.chain}] No healthy RPCs, using degraded by priority"
                )
                return min(degraded, key=lambda ep: ep.priority)

            # 3. Последняя надежда - любой доступный
            logger.warning(
                f"[{self.chain}] All RPCs degraded/unhealthy, using any available"
            )
            return min(available, key=lambda ep: ep.priority)

        return available[0]

    async def get_web3(self) -> AsyncWeb3:
        """Получить Web3 instance для лучшего endpoint."""
        endpoint = self._select_endpoint()
        if endpoint is None:
            raise RuntimeError(f"No available RPC endpoints for {self.chain}")
        return endpoint.web3

    async def execute(
        self,
        operation: Callable[[AsyncWeb3], Any],
        timeout: float | None = None,
    ) -> Any:
        """
        Выполнить операцию с автоматическим retry и failover.

        Args:
            operation: Async callable принимающий Web3 instance
            timeout: Таймаут операции в секундах

        Returns:
            Результат операции

        Raises:
            RuntimeError: Если все endpoints недоступны
        """
        timeout = timeout or self.config.request_timeout_sec
        last_error: Exception | None = None
        tried_endpoints: set[str] = set()

        for attempt in range(self.config.max_retries):
            endpoint = self._select_endpoint()

            if endpoint is None:
                raise RuntimeError(f"No available RPC endpoints for {self.chain}")

            # Избегаем повторного использования failed endpoint в одном запросе
            if (
                endpoint.url in tried_endpoints
                and len(self._get_available_endpoints()) > 1
            ):
                # Пробуем следующий
                available = [
                    ep
                    for ep in self._get_available_endpoints()
                    if ep.url not in tried_endpoints
                ]
                if available:
                    endpoint = available[0]

            tried_endpoints.add(endpoint.url)

            try:
                start_time = time.time()

                result = await asyncio.wait_for(
                    operation(endpoint.web3),
                    timeout=timeout,
                )

                latency_ms = (time.time() - start_time) * 1000
                endpoint.record_success(latency_ms)

                return result

            except asyncio.TimeoutError as e:
                endpoint.record_failure(f"Timeout after {timeout}s")
                last_error = e
                logger.warning(f"[{self.chain}] RPC timeout on {endpoint.url}")

            except Web3RPCError as e:
                error_msg = str(e)
                endpoint.record_failure(error_msg)
                last_error = e
                logger.warning(
                    f"[{self.chain}] RPC error on {endpoint.url}: {error_msg[:100]}"
                )

            except Exception as e:
                error_msg = str(e)
                endpoint.record_failure(error_msg)
                last_error = e
                logger.warning(
                    f"[{self.chain}] Error on {endpoint.url}: {error_msg[:100]}"
                )

            # Delay before retry
            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(self.config.retry_delay_ms / 1000)

        raise RuntimeError(
            f"All RPC endpoints failed for {self.chain} after {self.config.max_retries} attempts. "
            f"Last error: {last_error}"
        )

    async def health_check(self) -> dict[str, RpcHealth]:
        """
        Проверить здоровье всех endpoints.

        Returns:
            Словарь url -> health status
        """
        results: dict[str, RpcHealth] = {}

        async def check_endpoint(endpoint: RpcEndpoint) -> None:
            try:
                start = time.time()
                block = await asyncio.wait_for(
                    endpoint.web3.eth.block_number,
                    timeout=10,
                )
                latency_ms = (time.time() - start) * 1000
                endpoint.record_success(latency_ms)
                results[endpoint.url] = endpoint.health

            except Exception as e:
                endpoint.record_failure(str(e))
                results[endpoint.url] = endpoint.health

        await asyncio.gather(*[check_endpoint(ep) for ep in self.endpoints])

        healthy = sum(1 for h in results.values() if h == RpcHealth.HEALTHY)
        logger.info(
            f"[{self.chain}] Health check: {healthy}/{len(self.endpoints)} healthy"
        )

        return results

    def get_stats(self) -> dict:
        """Получить статистику по всем endpoints."""
        return {
            "chain": self.chain,
            "strategy": self.config.strategy.value,
            "endpoints": [
                {
                    "url": ep.url[:50] + "..." if len(ep.url) > 50 else ep.url,
                    "priority": ep.priority,
                    "health": ep.health.value,
                    "avg_latency_ms": round(ep.avg_latency_ms, 2),
                    "total_requests": ep.total_requests,
                    "error_rate": f"{ep.error_rate * 100:.1f}%",
                    "consecutive_failures": ep.consecutive_failures,
                }
                for ep in self.endpoints
            ],
        }


# === Global RPC Manager Registry ===

_rpc_managers: dict[str, RpcManager] = {}
_managers_lock = asyncio.Lock()


def configure_rpc_endpoints(chain: str, urls: list[str]) -> RpcManager:
    """
    Сконфигурировать RPC endpoints для сети.

    Args:
        chain: Имя сети
        urls: Список RPC URLs (первый = высший приоритет)
        strategy: Стратегия ротации (по умолчанию HYBRID)

    Returns:
        Настроенный RpcManager
    """
    endpoints = [RpcEndpoint(url=url, priority=i + 1) for i, url in enumerate(urls)]

    manager = RpcManager(
        chain=chain,
        endpoints=endpoints,
        config=RpcManagerConfig(
            strategy=RotationStrategy.HYBRID,  # Лучшая стратегия для payment gateway
            max_retries=3,
        ),
    )

    _rpc_managers[chain] = manager
    return manager


def get_rpc_manager(chain: str) -> RpcManager:
    """Получить RpcManager для сети."""
    if chain not in _rpc_managers:
        raise ValueError(
            f"RPC manager not configured for {chain}. Call configure_rpc_endpoints first."
        )
    return _rpc_managers[chain]


async def init_all_rpc_managers(rpc_config: dict[str, list[str]]) -> None:
    """
    Инициализировать RPC managers для всех сетей.

    Args:
        rpc_config: Словарь chain -> list[rpc_urls]

    Example:
        await init_all_rpc_managers({
            "arbitrum": [
                "https://arb1.arbitrum.io/rpc",
                "https://arb-mainnet.g.alchemy.com/v2/KEY",
            ],
            "base": [
                "https://mainnet.base.org",
                "https://base-mainnet.g.alchemy.com/v2/KEY",
            ],
        })
    """
    for chain, urls in rpc_config.items():
        configure_rpc_endpoints(chain, urls)
        logger.info(f"Configured RPC manager for {chain} with {len(urls)} endpoints")

    # Initial health check
    for manager in _rpc_managers.values():
        await manager.health_check()


async def close_all_rpc_managers() -> None:
    """Закрыть все RPC соединения."""
    # AsyncWeb3 не требует явного закрытия, но можем очистить
    _rpc_managers.clear()
    logger.info("All RPC managers closed")
