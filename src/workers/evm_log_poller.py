"""
EVM Log Poller Worker.
Сканирует блокчейн на предмет Transfer событий на deposit адреса.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from src.blockchain.chains import (
    get_all_chains,
    get_chain_config,
    get_transfer_event_signature,
)
from src.blockchain.evm_adapter import EvmAdapter, close_all_adapters, get_evm_adapter
from src.blockchain.oklink_client import (
    OKLinkClientConfig,
    OKLinkExplorerClient,
    OKLinkTransferLogFetcher,
)
from src.core.config import get_settings
from src.db.models import (
    ChainCheckpoint,
    DepositAddress,
    DepositAddressLeaseStatus,
    InvoiceStatus,
    OnchainTx,
    PaymentSession,
    PaymentSessionStatus,
    TxStatus,
)
from src.db.session import get_session_context
from src.services.invoice_service import InvoiceService
from src.services.payment_service import PaymentService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferLog:
    """Распарсенный Transfer event из OKLink/Web3 log."""

    tx_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    token_contract: str
    amount: Decimal


def _hex_or_int(value, default: int = 0) -> int:
    """Parse JSON-RPC hex strings, bytes-like values, or ints."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int(value.hex(), 16) if value else default
    if hasattr(value, "hex"):
        return int(value.hex(), 16)
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return default


def _parse_transfer_log(chain: str, log: dict) -> TransferLog | None:
    """Parse raw Web3-compatible Transfer log."""
    config = get_chain_config(chain)
    topics = log.get("topics", [])
    if len(topics) < 3:
        return None

    tx_hash = log.get("transactionHash", "")
    if isinstance(tx_hash, bytes):
        tx_hash = "0x" + tx_hash.hex()
    elif hasattr(tx_hash, "hex"):
        tx_hash = "0x" + tx_hash.hex()
    if tx_hash and not str(tx_hash).startswith("0x"):
        tx_hash = "0x" + str(tx_hash)

    topic1 = topics[1]
    topic2 = topics[2]
    from_address = _topic_to_address(topic1)
    to_address = _topic_to_address(topic2)

    data = log.get("data", "0x0")
    if isinstance(data, bytes):
        raw_amount = int(data.hex(), 16) if data else 0
    elif hasattr(data, "hex"):
        raw_amount = int(data.hex(), 16)
    else:
        raw_amount = int(data, 16) if data else 0

    token_contract = log.get("address", "")
    if isinstance(token_contract, bytes):
        token_contract = "0x" + token_contract.hex()
    elif hasattr(token_contract, "hex"):
        token_contract = "0x" + token_contract.hex()
    token_contract = str(token_contract).lower()

    decimals = 18
    for token in config.tokens.values():
        if token.contract_address.lower() == token_contract:
            decimals = token.decimals
            break

    return TransferLog(
        tx_hash=str(tx_hash),
        log_index=_hex_or_int(log.get("logIndex"), 0),
        block_number=_hex_or_int(log.get("blockNumber"), 0),
        from_address=from_address,
        to_address=to_address,
        token_contract=token_contract,
        amount=Decimal(raw_amount) / Decimal(10**decimals),
    )


def _topic_to_address(topic) -> str:
    if isinstance(topic, bytes):
        return "0x" + topic.hex()[-40:]
    if hasattr(topic, "hex"):
        return "0x" + topic.hex()[-40:]
    return "0x" + str(topic)[-40:]


def _build_oklink_fetcher(chain: str, config) -> OKLinkTransferLogFetcher:
    """Build OKLink transfer fetcher for active payment checks."""
    settings = get_settings()
    oklink_chain = str(getattr(config, "oklink_chain", "") or "").strip()
    if not oklink_chain:
        raise RuntimeError(f"[{chain}] oklink_chain is required for OKLink scanner")

    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url=settings.oklink_base_url,
            api_prefix=settings.oklink_api_prefix,
            referer=settings.oklink_referer,
            user_agent=settings.oklink_user_agent,
            web_key=settings.oklink_web_key.get_secret_value(),
            transfer_event_signature=get_transfer_event_signature(),
            page_limit=int(getattr(config, "scanner_page_limit", 0)),
            request_timeout_seconds=settings.oklink_request_timeout_seconds,
            request_delay_seconds=(
                int(getattr(config, "scanner_request_delay_ms", 0)) / 1000
            ),
            max_pages_per_address=int(
                getattr(config, "scanner_max_pages_per_address", 0)
            ),
            max_log_pages_per_tx=int(getattr(config, "scanner_max_log_pages_per_tx", 0)),
            api_key_time_shift_ms=settings.oklink_api_key_time_shift_ms,
        )
    )
    return OKLinkTransferLogFetcher(oklink_chain, client)


async def get_or_create_checkpoint(
    session,
    chain: str,
    adapter: EvmAdapter,
    earliest_invoice_time: datetime | None = None,
) -> int:
    """Получить или создать checkpoint для сети."""
    stmt = select(ChainCheckpoint).where(ChainCheckpoint.chain == chain)
    result = await session.execute(stmt)
    checkpoint = result.scalar_one_or_none()

    if checkpoint is not None:
        return checkpoint.last_scanned_block

    # Создаём новый checkpoint
    current_block = await adapter.get_latest_block_number()
    config = get_chain_config(chain)

    # Вычисляем начальный блок с учётом возраста самого старого инвойса
    start_block = current_block

    if earliest_invoice_time:
        if earliest_invoice_time.tzinfo is None:
            earliest_invoice_time = earliest_invoice_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_seconds = max(0, int((now - earliest_invoice_time).total_seconds()))
        # Добавляем буфер в 60 секунд на случай задержки создания инвойса
        age_seconds += 60
        blocks_back = int(age_seconds / config.block_time_sec) + config.reorg_buffer
        start_block = current_block - blocks_back

        # Убеждаемся что start_block разумный (не уходим в далёкое прошлое)
        # Минимум — текущий блок минус 1 час блоков
        min_start_block = current_block - int(3600 / config.block_time_sec)
        start_block = max(start_block, min_start_block)

        logger.info(
            f"[{chain}] Creating new checkpoint at block {start_block} "
            f"(current={current_block}, oldest invoice age: {age_seconds}s, "
            f"blocks back: {blocks_back})"
        )

    checkpoint = ChainCheckpoint(
        chain=chain,
        last_scanned_block=start_block,
    )
    session.add(checkpoint)
    await session.commit()
    return start_block


async def update_checkpoint(session, chain: str, block_number: int) -> None:
    """Обновить checkpoint для сети."""
    stmt = select(ChainCheckpoint).where(ChainCheckpoint.chain == chain)
    result = await session.execute(stmt)
    checkpoint = result.scalar_one_or_none()

    if checkpoint:
        checkpoint.last_scanned_block = block_number
        checkpoint.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_active_deposit_addresses(
    session, chain: str
) -> dict[str, PaymentSession]:
    """
    Получить все активные deposit адреса для сети.
    Возвращает словарь: address -> PaymentSession
    """
    now = datetime.now(timezone.utc)
    active_window = and_(
        PaymentSession.status.in_(
            [
                PaymentSessionStatus.PENDING,
                PaymentSessionStatus.SEEN_ONCHAIN,
            ]
        ),
        PaymentSession.expires_at > now,
    )
    late_window = and_(
        PaymentSession.status.in_(
            [
                PaymentSessionStatus.EXPIRED,
                PaymentSessionStatus.LATE,
            ]
        ),
        DepositAddress.lease_status == DepositAddressLeaseStatus.COOLDOWN,
        DepositAddress.cooldown_until > now,
    )

    stmt = (
        select(PaymentSession)
        .join(DepositAddress, PaymentSession.deposit_address_id == DepositAddress.id)
        .options(
            selectinload(PaymentSession.deposit_address),
            selectinload(PaymentSession.invoice),
        )
        .where(
            and_(
                PaymentSession.chain == chain,
                or_(active_window, late_window),
            )
        )
    )
    result = await session.execute(stmt)
    sessions = result.scalars().all()

    address_map: dict[str, PaymentSession] = {}
    for payment_session in sessions:
        if not payment_session.deposit_address:
            continue
        address = payment_session.deposit_address.address.lower()
        current = address_map.get(address)
        if current is None or _session_recency(payment_session) > _session_recency(current):
            address_map[address] = payment_session
    return address_map


def _session_recency(payment_session: PaymentSession) -> datetime:
    recency = (
        payment_session.released_at
        or payment_session.paid_at
        or payment_session.chosen_at
    )
    if recency is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if recency.tzinfo is None:
        return recency.replace(tzinfo=timezone.utc)
    return recency


def _active_invoice_from_block(
    head_block: int,
    config,
    earliest_invoice_time: datetime | None,
) -> int:
    """Lower bound for active invoice address scan, independent of checkpoint."""
    if earliest_invoice_time is None:
        return max(0, head_block - int(getattr(config, "scan_window", 2000)))
    if earliest_invoice_time.tzinfo is None:
        earliest_invoice_time = earliest_invoice_time.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_seconds = max(0, int((now - earliest_invoice_time).total_seconds()))
    age_seconds += 120
    block_time = max(1, int(getattr(config, "block_time_sec", 1)))
    blocks_back = int(age_seconds / block_time) + int(getattr(config, "reorg_buffer", 0))
    return max(0, head_block - blocks_back)


async def poll_chain(chain: str) -> None:
    """
    Сканировать блокчейн на предмет Transfer событий.
    """
    config = get_chain_config(chain)

    # Сначала проверяем есть ли активные адреса - если нет, не делаем RPC вызовы
    async with get_session_context() as session:
        # Получаем активные deposit адреса ДО любых RPC вызовов
        address_map = await get_active_deposit_addresses(session, chain)

        if not address_map:
            logger.debug(f"[{chain}] No active deposit addresses, skipping chain")
            return

        # Вычисляем время самого старого инвойса ДО создания checkpoint
        earliest_invoice_time = min(
            (ps.invoice.created_at for ps in address_map.values() if ps.invoice),
            default=None,
        )

        # Используем кэшированный adapter
        adapter = get_evm_adapter(chain)

        # Получаем текущий head блок
        head_block = await adapter.get_latest_block_number()

        # Вычисляем safe block (с учётом reorg buffer)
        safe_block = head_block - config.reorg_buffer
        if safe_block <= 0:
            return

        scanner_provider = str(getattr(config, "scanner_provider", "rpc")).lower()
        last_scanned: int | None = None

        if scanner_provider == "oklink":
            # Invoice-flow сканирует active addresses напрямую. Stale checkpoint
            # не должен скрывать свежий payment, поэтому lower bound считается
            # от возраста active invoice, а не от chain_checkpoints.
            from_block = _active_invoice_from_block(
                safe_block,
                config,
                earliest_invoice_time,
            )
            to_block = safe_block
        else:
            # Legacy/RPC path остаётся checkpoint-based.
            last_scanned = await get_or_create_checkpoint(
                session, chain, adapter, earliest_invoice_time
            )
            if safe_block <= last_scanned:
                logger.debug(
                    f"[{chain}] No new blocks to scan (last={last_scanned}, safe={safe_block})"
                )
                return
            to_block = min(safe_block, last_scanned + config.scan_window)
            from_block = last_scanned + 1

        logger.info(
            f"[{chain}] Scanning blocks {from_block} - {to_block} "
            f"(active addresses: {len(address_map)}, provider={scanner_provider})"
        )

        # Получаем адреса токенов
        token_contracts = [
            config.tokens["USDT"].contract_address,
            config.tokens["USDC"].contract_address,
        ]

        all_addresses = list(address_map.keys())
        oklink_fetcher = None
        scan_complete = True
        failed_address_count = 0

        try:
            if scanner_provider == "oklink":
                oklink_fetcher = _build_oklink_fetcher(chain, config)
                fetch_result = await oklink_fetcher.fetch_transfer_logs(
                    from_block=from_block,
                    to_block=to_block,
                    to_addresses=all_addresses,
                    token_contracts=token_contracts,
                )
                if not fetch_result.is_complete:
                    scan_complete = False
                    failed_address_count = fetch_result.failed_address_count
                    logger.warning(
                        f"[{chain}] OKLink active-check scan incomplete; "
                        f"processing partial results without checkpoint advance: "
                        f"failed_address_count={fetch_result.failed_address_count}"
                    )

                all_transfers = []
                for log in fetch_result.logs:
                    try:
                        transfer = _parse_transfer_log(chain, log)
                        if transfer is not None:
                            all_transfers.append(transfer)
                    except Exception as e:
                        logger.warning(f"[{chain}] Failed to parse OKLink log: {e}")
            elif scanner_provider == "rpc":
                batch_result = await adapter.get_transfer_logs_batch(
                    from_block=from_block,
                    to_block=to_block,
                    to_addresses=all_addresses,
                    token_contracts=token_contracts,
                )
                all_transfers = batch_result.transfers
                if not batch_result.is_complete:
                    scan_complete = False
                    failed_address_count = batch_result.failed_address_count
                    logger.warning(
                        f"[{chain}] RPC active-check scan incomplete; "
                        f"processing partial results without checkpoint advance: "
                        f"failed_address_count={batch_result.failed_address_count}"
                    )
            else:
                raise RuntimeError(
                    f"[{chain}] Unsupported scanner_provider={scanner_provider}"
                )
        except Exception as e:
            logger.error(f"[{chain}] Error fetching transfer logs: {e}")
            return
        finally:
            if oklink_fetcher is not None:
                await oklink_fetcher.aclose()

        if all_transfers:
            logger.info(
                f"[{chain}] Found {len(all_transfers)} transfers in blocks {from_block}-{to_block}"
            )
            for t in all_transfers:
                logger.debug(
                    f"[{chain}]   Transfer: to={t.to_address}, "
                    f"amount={t.amount}, token={t.token_contract}"
                )

        # Обрабатываем найденные трансферы
        for transfer in all_transfers:
            to_addr = transfer.to_address.lower()
            payment_session = address_map.get(to_addr)
            if not payment_session:
                continue

            try:
                # Проверяем токен (кэшируем expected_token для сессии)
                expected_token_addr = config.get_token(payment_session.token)
                if expected_token_addr is None:
                    continue

                if (
                    transfer.token_contract.lower()
                    != expected_token_addr.contract_address.lower()
                ):
                    continue

                # Проверяем сумму (с учётом разной точности Decimal)
                # Invoice amount может иметь больше знаков после запятой
                invoice = payment_session.invoice
                # Нормализуем до 6 знаков (USDT/USDC decimals)
                transfer_amount_normalized = transfer.amount.quantize(
                    Decimal("0.000001")
                )
                invoice_amount_normalized = invoice.amount.quantize(Decimal("0.000001"))

                if transfer_amount_normalized != invoice_amount_normalized:
                    logger.warning(
                        f"[{chain}] Amount mismatch for {to_addr}: "
                        f"expected={invoice.amount} ({invoice_amount_normalized}), "
                        f"got={transfer.amount} ({transfer_amount_normalized})"
                    )
                    continue

                logger.info(
                    f"[{chain}] Found matching transfer for {to_addr}: "
                    f"amount={transfer.amount}, tx={transfer.tx_hash}"
                )

                # Записываем транзакцию
                payment_service = PaymentService(session)
                await payment_service.record_onchain_tx(
                    payment_session=payment_session,
                    tx_hash=transfer.tx_hash,
                    block_number=transfer.block_number,
                    log_index=transfer.log_index,
                    from_address=transfer.from_address,
                    amount=transfer.amount,
                )

                # Обновляем статус payment session и инвойса на SEEN_ONCHAIN
                if payment_session.status == PaymentSessionStatus.PENDING:
                    payment_session.status = PaymentSessionStatus.SEEN_ONCHAIN

                if invoice.status == InvoiceStatus.AWAITING_PAYMENT:
                    invoice_service = InvoiceService(session)
                    await invoice_service.update_invoice_status(
                        invoice,
                        InvoiceStatus.SEEN_ONCHAIN,
                        event_payload={
                            "tx_hash": transfer.tx_hash,
                            "block_number": transfer.block_number,
                        },
                    )

                logger.info(
                    f"[{chain}] Found transfer to {to_addr}: "
                    f"tx={transfer.tx_hash}, amount={transfer.amount}"
                )

            except Exception as e:
                logger.error(f"[{chain}] Error processing transfer to {to_addr}: {e}")

        # Обновляем checkpoint только после полного скана.
        if scan_complete:
            await update_checkpoint(session, chain, to_block)
        else:
            logger.warning(
                f"[{chain}] Active-check checkpoint not advanced: "
                f"failed_address_count={failed_address_count}"
            )


async def update_confirmations(chain: str) -> None:
    """
    Обновить количество подтверждений для транзакций в процессе подтверждения.
    """
    config = get_chain_config(chain)

    async with get_session_context() as session:
        # Получаем транзакции в процессе подтверждения
        stmt = (
            select(OnchainTx)
            .options(
                selectinload(OnchainTx.payment_session).selectinload(
                    PaymentSession.invoice
                ),
                selectinload(OnchainTx.payment_session).selectinload(
                    PaymentSession.deposit_address
                ),
            )
            .where(
                and_(
                    OnchainTx.chain == chain,
                    OnchainTx.status.in_([TxStatus.PENDING, TxStatus.CONFIRMING]),
                )
            )
        )
        result = await session.execute(stmt)
        txs = result.scalars().all()

        if not txs:
            return

        # Используем кэшированный adapter
        adapter = get_evm_adapter(chain)

        # Получаем текущий head блок
        head_block = await adapter.get_latest_block_number()

        for tx in txs:
            try:
                confirmations = head_block - tx.block_number + 1

                payment_service = PaymentService(session)
                is_confirmed = await payment_service.update_tx_confirmations(
                    tx,
                    confirmations,
                    config.confirmations,
                )

                if is_confirmed:
                    payment_session = tx.payment_session
                    invoice = payment_session.invoice
                    if payment_session.status == PaymentSessionStatus.LATE:
                        await payment_service.process_late_payment(
                            payment_session,
                            invoice,
                        )
                        logger.warning(
                            f"[{chain}] Late transaction {tx.tx_hash} confirmed"
                        )
                    elif invoice.status == InvoiceStatus.EXPIRED:
                        await payment_service.process_late_payment(
                            payment_session,
                            invoice,
                        )
                        logger.warning(
                            f"[{chain}] Expired-invoice transaction "
                            f"{tx.tx_hash} confirmed as late"
                        )
                    else:
                        await payment_service.process_confirmed_payment(
                            payment_session,
                            invoice,
                        )
                        logger.info(f"[{chain}] Transaction {tx.tx_hash} confirmed!")
                else:
                    logger.debug(
                        f"[{chain}] Transaction {tx.tx_hash}: "
                        f"{confirmations}/{config.confirmations} confirmations"
                    )

            except Exception as e:
                logger.error(
                    f"[{chain}] Error updating confirmations for {tx.tx_hash}: {e}"
                )


async def run_poller() -> None:
    """
    Главный цикл EVM Log Poller.
    Запускает polling для всех сетей.
    """
    settings = get_settings()
    chains = get_all_chains()

    logger.info(f"Starting EVM Log Poller for chains: {chains}")

    try:
        while True:
            for chain in chains:
                try:
                    # Сканируем новые блоки
                    await poll_chain(chain)

                    # Обновляем подтверждения
                    await update_confirmations(chain)

                except Exception as e:
                    logger.error(f"Error polling {chain}: {e}")

            # Пауза между итерациями
            await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        # Закрываем все adapter sessions при выходе
        await close_all_adapters()


# ARQ Worker Settings
class WorkerSettings:
    """Настройки для ARQ worker."""

    functions = []  # Функции не используем, это standalone worker
    on_startup = None
    on_shutdown = None

    @staticmethod
    async def run_worker():
        """Запуск worker."""
        await run_poller()


if __name__ == "__main__":
    # Запуск как standalone скрипт
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_poller())
