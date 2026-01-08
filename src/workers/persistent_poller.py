"""
Persistent Deposit Poller Worker.

Сканирует блокчейн на предмет Transfer событий на ПОСТОЯННЫЕ deposit адреса.
В отличие от invoice poller, этот воркер отслеживает все активные wallet addresses бессрочно.

Использует ResilientLogFetcher для максимальной отказоустойчивости:
1. Primary RPC + OR Topics (1 запрос на все адреса)
2. Secondary RPC + OR Topics (failover)
3. Primary RPC + Parallel Batching
4. Secondary RPC + Parallel Batching
5. Sequential fallback
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, select, update
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_all_chains, get_chain_config, get_evm_chains
from src.blockchain.evm_adapter import get_evm_adapter, close_all_adapters
from src.blockchain.resilient_fetcher import (
    ResilientLogFetcher,
    get_resilient_fetcher,
    init_resilient_fetchers,
    close_resilient_fetchers,
)
from src.blockchain.rpc_manager import RpcManager, RpcEndpoint, RpcManagerConfig
from src.core.config import get_settings
from src.db.models import (
    Deposit,
    DepositStatus,
    OutboxStatus,
    OutboxWebhook,
    UserWallet,
    WalletAddress,
    Webhook,
)
from src.db.session import get_session_context
from src.services.user_wallet_service import (
    UserWalletService,
    get_all_active_wallet_addresses,
)

logger = logging.getLogger(__name__)


# === Transfer Log Parsing ===

from dataclasses import dataclass


@dataclass
class TransferLog:
    """Распарсенный Transfer event."""

    tx_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    token_contract: str
    amount: Decimal
    raw_amount: int


def _parse_transfer_log(chain: str, log: dict) -> TransferLog | None:
    """Распарсить Transfer event лог из raw dict."""
    config = get_chain_config(chain)

    topics = log.get("topics", [])
    if len(topics) < 3:
        return None

    # Transaction hash
    tx_hash = log.get("transactionHash", "")
    if isinstance(tx_hash, bytes):
        tx_hash = tx_hash.hex()
    elif hasattr(tx_hash, "hex"):
        tx_hash = tx_hash.hex()

    # Parse topics
    topic1 = topics[1]
    topic2 = topics[2]

    if isinstance(topic1, bytes):
        from_address = "0x" + topic1.hex()[-40:]
    elif hasattr(topic1, "hex"):
        from_address = "0x" + topic1.hex()[-40:]
    else:
        from_address = "0x" + str(topic1)[-40:]

    if isinstance(topic2, bytes):
        to_address = "0x" + topic2.hex()[-40:]
    elif hasattr(topic2, "hex"):
        to_address = "0x" + topic2.hex()[-40:]
    else:
        to_address = "0x" + str(topic2)[-40:]

    # Parse data (amount)
    data = log.get("data", "0x0")
    if isinstance(data, bytes):
        raw_amount = int(data.hex(), 16) if data else 0
    elif hasattr(data, "hex"):
        raw_amount = int(data.hex(), 16)
    else:
        raw_amount = int(data, 16) if data else 0

    # Token contract
    token_contract = log.get("address", "")
    if isinstance(token_contract, bytes):
        token_contract = token_contract.hex()
    elif hasattr(token_contract, "hex"):
        token_contract = "0x" + token_contract.hex()
    token_contract = token_contract.lower()

    # Get decimals
    decimals = 18
    for token in config.tokens.values():
        if token.contract_address.lower() == token_contract:
            decimals = token.decimals
            break

    amount = Decimal(raw_amount) / Decimal(10**decimals)

    return TransferLog(
        tx_hash=tx_hash,
        log_index=log.get("logIndex", 0),
        block_number=log.get("blockNumber", 0),
        from_address=from_address,
        to_address=to_address,
        token_contract=token_contract,
        amount=amount,
        raw_amount=raw_amount,
    )


# === Main Polling Functions ===


async def poll_persistent_deposits(chain: str) -> int:
    """
    Сканировать блокчейн на предмет Transfer событий на persistent адреса.

    Использует ResilientLogFetcher для максимальной отказоустойчивости:
    1. Primary RPC + OR Topics
    2. Secondary RPC + OR Topics
    3. Primary + Parallel Batch
    4. Secondary + Parallel Batch
    5. Sequential fallback

    Returns:
        Количество найденных депозитов
    """
    config = get_chain_config(chain)
    deposits_found = 0

    async with get_session_context() as session:
        # Получаем все активные wallet addresses для сети
        address_map = await get_all_active_wallet_addresses(session, chain)

        if not address_map:
            logger.debug(f"[{chain}] No active wallet addresses, skipping")
            return 0

        logger.debug(f"[{chain}] Scanning {len(address_map)} persistent addresses")

        # Получаем adapter для block number
        adapter = get_evm_adapter(chain)

        # Получаем текущий блок
        head_block = await adapter.get_latest_block_number()
        safe_block = head_block - config.reorg_buffer

        # Определяем начальный блок для сканирования
        # Используем минимальный last_scanned_block среди всех адресов
        min_scanned = min(
            (addr.last_scanned_block or 0 for addr in address_map.values()),
            default=0,
        )

        # Если нет checkpoint — начинаем с текущего блока минус 1 час
        if min_scanned == 0:
            blocks_per_hour = int(3600 / config.block_time_sec)
            min_scanned = max(0, safe_block - blocks_per_hour)

        if safe_block <= min_scanned:
            logger.debug(f"[{chain}] No new blocks to scan")
            return 0

        from_block = min_scanned + 1
        to_block = min(safe_block, from_block + config.scan_window - 1)

        logger.info(
            f"[{chain}] Scanning persistent addresses: blocks {from_block}-{to_block}"
        )

        # Получаем адреса токенов
        token_contracts = [
            config.tokens["USDT"].contract_address,
            config.tokens["USDC"].contract_address,
        ]

        try:
            # Используем ResilientLogFetcher для получения логов
            fetcher = get_resilient_fetcher(chain)
            all_addresses = list(address_map.keys())

            if fetcher:
                # Используем resilient fetcher с OR Topics + fallback
                result = await fetcher.fetch_transfer_logs(
                    from_block=from_block,
                    to_block=to_block,
                    to_addresses=all_addresses,
                    token_contracts=token_contracts,
                )

                raw_logs = result.logs
                logger.debug(
                    f"[{chain}] Fetched {len(raw_logs)} logs via {result.method_used.value} "
                    f"in {result.latency_ms:.0f}ms"
                )

                # Парсим логи в TransferLog
                transfers = []
                for log in raw_logs:
                    try:
                        transfer = _parse_transfer_log(chain, log)
                        if transfer:
                            transfers.append(transfer)
                    except Exception as e:
                        logger.warning(f"[{chain}] Failed to parse log: {e}")
            else:
                # Fallback на старый метод через adapter
                logger.warning(
                    f"[{chain}] ResilientLogFetcher not available, using adapter"
                )
                transfers = await adapter.get_transfer_logs_batch(
                    from_block=from_block,
                    to_block=to_block,
                    to_addresses=all_addresses,
                    token_contracts=token_contracts,
                )
        except Exception as e:
            logger.error(f"[{chain}] Error fetching transfer logs: {e}")
            return 0

        if transfers:
            logger.info(f"[{chain}] Found {len(transfers)} transfers")

        # Обрабатываем найденные трансферы
        wallet_service = UserWalletService(session)

        for transfer in transfers:
            to_addr = transfer.to_address.lower()
            wallet_address = address_map.get(to_addr)

            if not wallet_address:
                continue

            # Определяем asset
            asset = None
            for asset_name, token_config in config.tokens.items():
                if (
                    transfer.token_contract.lower()
                    == token_config.contract_address.lower()
                ):
                    asset = asset_name
                    break

            if not asset:
                continue

            try:
                # Записываем депозит
                deposit = await wallet_service.record_deposit(
                    wallet_address=wallet_address,
                    tx_hash=transfer.tx_hash,
                    block_number=transfer.block_number,
                    log_index=transfer.log_index,
                    amount=transfer.amount,
                    asset=asset,
                    token_contract=transfer.token_contract,
                    from_address=transfer.from_address,
                    required_confirmations=config.confirmations,
                )

                # deposit может быть None если отклонён по безопасности
                if deposit is None:
                    logger.warning(
                        f"[{chain}] Deposit REJECTED by security validation: "
                        f"{transfer.amount} {asset} (tx={transfer.tx_hash[:16]}...)"
                    )
                    continue

                if deposit.status == DepositStatus.PENDING:
                    deposits_found += 1
                    logger.info(
                        f"[{chain}] New deposit: {transfer.amount} {asset} "
                        f"to {to_addr[:10]}... (tx={transfer.tx_hash[:16]}...)"
                    )

            except Exception as e:
                logger.error(f"[{chain}] Error recording deposit: {e}")

        # Обновляем last_scanned_block для всех адресов
        stmt = (
            update(WalletAddress)
            .where(
                and_(
                    WalletAddress.chain == chain,
                    WalletAddress.is_active == True,
                )
            )
            .values(last_scanned_block=to_block)
        )
        await session.execute(stmt)
        await session.commit()

    return deposits_found


async def update_deposit_confirmations(chain: str) -> int:
    """
    Обновить подтверждения для pending депозитов.

    Returns:
        Количество подтверждённых депозитов
    """
    config = get_chain_config(chain)
    confirmed_count = 0

    async with get_session_context() as session:
        # Получаем pending/confirming депозиты
        stmt = (
            select(Deposit)
            .options(
                selectinload(Deposit.user_wallet).selectinload(UserWallet.merchant),
                selectinload(Deposit.wallet_address),
            )
            .where(
                and_(
                    Deposit.chain == chain,
                    Deposit.status.in_(
                        [DepositStatus.PENDING, DepositStatus.CONFIRMING]
                    ),
                )
            )
            .limit(100)
        )
        result = await session.execute(stmt)
        deposits = result.scalars().all()

        if not deposits:
            return 0

        adapter = get_evm_adapter(chain)
        current_block = await adapter.get_latest_block_number()

        wallet_service = UserWalletService(session)

        for deposit in deposits:
            try:
                is_confirmed = await wallet_service.update_deposit_confirmations(
                    deposit, current_block
                )

                if is_confirmed:
                    confirmed_count += 1

                    # Создаём webhook для мерчанта
                    await create_deposit_webhook(session, deposit)

            except Exception as e:
                logger.error(
                    f"[{chain}] Error updating confirmations for {deposit.tx_hash}: {e}"
                )

        await session.commit()

    return confirmed_count


async def create_deposit_webhook(session, deposit: Deposit) -> None:
    """Создать webhook для подтверждённого депозита."""
    # Находим webhook мерчанта
    stmt = select(Webhook).where(
        and_(
            Webhook.merchant_id == deposit.user_wallet.merchant_id,
            Webhook.is_active == True,
        )
    )
    result = await session.execute(stmt)
    webhooks = result.scalars().all()

    for webhook in webhooks:
        # Проверяем подписку на deposit.received
        if "deposit.received" in webhook.events or "*" in webhook.events:
            payload = {
                "event": "deposit.received",
                "data": {
                    "deposit_id": str(deposit.id),
                    "external_user_id": deposit.user_wallet.external_user_id,
                    "chain": deposit.chain,
                    "tx_hash": deposit.tx_hash,
                    "amount": str(deposit.amount),
                    "asset": deposit.asset,
                    "from_address": deposit.from_address,
                    "confirmations": deposit.confirmations,
                    "confirmed_at": (
                        deposit.confirmed_at.isoformat()
                        if deposit.confirmed_at
                        else None
                    ),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            outbox = OutboxWebhook(
                id=uuid.uuid4(),
                webhook_id=webhook.id,
                invoice_id=None,  # Deposit, не invoice
                event_type="deposit.received",
                payload=payload,
                status=OutboxStatus.PENDING,
            )
            session.add(outbox)

            logger.info(f"Created webhook for deposit {deposit.id} to {webhook.url}")


async def _init_resilient_fetchers() -> None:
    """Инициализировать ResilientLogFetcher и RpcManager для всех сетей."""
    settings = get_settings()

    rpc_config: dict[str, list[str]] = {}

    for chain in get_all_chains():
        urls = settings.get_rpc_urls(chain)
        if urls:
            rpc_config[chain] = urls
            logger.info(
                f"[{chain}] Configured {len(urls)} RPC endpoints for resilient fetcher"
            )

    if rpc_config:
        await init_resilient_fetchers(rpc_config)
        logger.info(f"ResilientLogFetcher initialized for {len(rpc_config)} chains")

    # Инициализируем RpcManager для EVM адаптеров (для get_latest_block и др.)
    for chain in get_evm_chains():
        urls = settings.get_rpc_urls(chain)
        if urls and len(urls) > 1:
            # Создаём RpcManager с multiple endpoints
            endpoints = [
                RpcEndpoint(url=url, priority=i + 1)
                for i, url in enumerate(urls)
            ]
            rpc_manager = RpcManager(
                chain=chain,
                endpoints=endpoints,
                config=RpcManagerConfig(max_retries=3),
            )
            # Подключаем к адаптеру
            adapter = get_evm_adapter(chain)
            adapter.set_rpc_manager(rpc_manager)
            logger.info(f"[{chain}] RpcManager attached with {len(urls)} endpoints")


async def run_persistent_poller() -> None:
    """
    Главный цикл Persistent Deposit Poller.

    Использует ResilientLogFetcher для максимальной отказоустойчивости:
    - Primary RPC + OR Topics (1 запрос)
    - Secondary RPC + OR Topics (failover)
    - Parallel Batching (если OR Topics не поддерживается)
    - Sequential fallback (последняя надежда)
    """
    settings = get_settings()
    chains = get_all_chains()

    logger.info(f"Starting Persistent Deposit Poller for chains: {chains}")

    # Инициализируем ResilientLogFetcher
    await _init_resilient_fetchers()

    iteration = 0
    try:
        while True:
            iteration += 1

            for chain in chains:
                try:
                    # Сканируем новые депозиты
                    new_deposits = await poll_persistent_deposits(chain)

                    # Обновляем подтверждения
                    confirmed = await update_deposit_confirmations(chain)

                    if new_deposits or confirmed:
                        logger.info(
                            f"[{chain}] Deposits: +{new_deposits} new, "
                            f"+{confirmed} confirmed"
                        )

                except Exception as e:
                    logger.error(f"[{chain}] Error in persistent poller: {e}")

            # Логируем статистику каждые 100 итераций
            if iteration % 100 == 0:
                for chain in chains:
                    fetcher = get_resilient_fetcher(chain)
                    if fetcher:
                        stats = fetcher.get_stats()
                        for ep in stats["endpoints"]:
                            logger.info(
                                f"[{chain}] RPC stats: latency={ep['avg_latency_ms']}ms, "
                                f"requests={ep['total_requests']}, "
                                f"or_topics={ep['supports_or_topics']}, "
                                f"circuit_or={ep['circuit_or_topics']}, "
                                f"circuit_parallel={ep['circuit_parallel']}"
                            )

            # Пауза между итерациями
            await asyncio.sleep(settings.poll_interval_seconds)

    finally:
        close_resilient_fetchers()
        await close_all_adapters()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_persistent_poller())
