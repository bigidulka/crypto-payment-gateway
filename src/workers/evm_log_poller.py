"""
EVM Log Poller Worker.
Сканирует блокчейн на предмет Transfer событий на deposit адреса.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_all_chains, get_chain_config
from src.blockchain.evm_adapter import EvmAdapter, close_all_adapters, get_evm_adapter
from src.core.config import get_settings
from src.db.models import (
    ChainCheckpoint,
    Invoice,
    InvoiceStatus,
    OnchainTx,
    PaymentSession,
    TxStatus,
)
from src.db.session import get_session_context
from src.services.invoice_service import InvoiceService
from src.services.payment_service import PaymentService

logger = logging.getLogger(__name__)


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
            f"(current={current_block}, oldest invoice age: {age_seconds}s, blocks back: {blocks_back})"
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
    stmt = (
        select(PaymentSession)
        .join(Invoice)
        .options(
            selectinload(PaymentSession.deposit_address),
            selectinload(PaymentSession.invoice),
        )
        .where(
            and_(
                PaymentSession.chain == chain,
                Invoice.status.in_(
                    [InvoiceStatus.AWAITING_PAYMENT, InvoiceStatus.SEEN_ONCHAIN]
                ),
                Invoice.expires_at > datetime.now(timezone.utc),
            )
        )
    )
    result = await session.execute(stmt)
    sessions = result.scalars().all()

    return {
        ps.deposit_address.address.lower(): ps for ps in sessions if ps.deposit_address
    }


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

        # Получаем checkpoint (передаём earliest_invoice_time для правильного начального блока)
        last_scanned = await get_or_create_checkpoint(
            session, chain, adapter, earliest_invoice_time
        )

        # Получаем текущий head блок
        head_block = await adapter.get_latest_block_number()

        # Вычисляем safe block (с учётом reorg buffer)
        safe_block = head_block - config.reorg_buffer

        if safe_block <= last_scanned:
            logger.debug(
                f"[{chain}] No new blocks to scan (last={last_scanned}, safe={safe_block})"
            )
            return

        # Ограничиваем окно сканирования
        to_block = min(safe_block, last_scanned + config.scan_window)
        from_block = last_scanned + 1

        logger.info(
            f"[{chain}] Scanning blocks {from_block} - {to_block} (active addresses: {len(address_map)})"
        )

        # Получаем адреса токенов
        token_contracts = [
            config.tokens["USDT"].contract_address,
            config.tokens["USDC"].contract_address,
        ]

        # ОПТИМИЗАЦИЯ: Получаем ВСЕ Transfer логи за один RPC вызов
        # вместо N вызовов для каждого адреса
        all_addresses = list(address_map.keys())

        try:
            all_transfers = await adapter.get_transfer_logs_batch(
                from_block=from_block,
                to_block=to_block,
                to_addresses=all_addresses,
                token_contracts=token_contracts,
            )
        except Exception as e:
            logger.error(f"[{chain}] Error fetching transfer logs: {e}")
            return

        if all_transfers:
            logger.info(
                f"[{chain}] Found {len(all_transfers)} transfers in blocks {from_block}-{to_block}"
            )
            for t in all_transfers:
                logger.debug(
                    f"[{chain}]   Transfer: to={t.to_address}, amount={t.amount}, token={t.token_contract}"
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
                onchain_tx = await payment_service.record_onchain_tx(
                    payment_session=payment_session,
                    tx_hash=transfer.tx_hash,
                    block_number=transfer.block_number,
                    log_index=transfer.log_index,
                    from_address=transfer.from_address,
                    amount=transfer.amount,
                )

                # Обновляем статус инвойса на SEEN_ONCHAIN
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

        # Обновляем checkpoint
        await update_checkpoint(session, chain, to_block)


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
                )
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
                    # Обрабатываем подтверждённый платёж
                    await payment_service.process_confirmed_payment(
                        tx.payment_session,
                        tx.payment_session.invoice,
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
