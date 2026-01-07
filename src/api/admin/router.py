"""
Admin API Router.
Эндпоинты для администрирования системы.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, desc, and_
from sqlalchemy.orm import selectinload

from src.api.admin.schemas import (
    ActionResponse,
    ChainStatus,
    CheckAllBalancesResponse,
    DashboardStats,
    FunderStatus,
    InvoiceFilters,
    InvoiceListItem,
    InvoiceListResponse,
    LoginRequest,
    LoginResponse,
    MerchantListItem,
    MerchantListResponse,
    ResetSweepRequest,
    RetrySweepRequest,
    SweepListResponse,
    SweepListItem,
    SystemLogEntry,
    SystemLogsResponse,
    SystemStatusResponse,
    WalletBalanceItem,
    WorkerStatus,
)
from src.api.deps import SessionDep
from src.blockchain.chains import get_all_chains, get_chain_config
from src.blockchain.evm_adapter import get_evm_adapter
from src.core.config import get_settings
from src.db.models import (
    ChainCheckpoint,
    Invoice,
    InvoiceStatus,
    Merchant,
    ApiKey,
    OnchainTx,
    PaymentSession,
    SweepJob,
    SweepState,
    SystemLog,
    SystemLogLevel,
)
from src.services.system_logger import SystemLogger

router = APIRouter(tags=["Admin"])


# === Authentication ===


@router.post("/login", response_model=LoginResponse, include_in_schema=False)
async def admin_login(request: LoginRequest) -> LoginResponse:
    """
    Простая авторизация для админки.
    Логин: admin, Пароль: admin
    """
    if request.username == "admin" and request.password == "admin":
        # Генерируем простой токен (в продакшене использовать JWT)
        import secrets

        token = secrets.token_urlsafe(32)
        return LoginResponse(success=True, token=token)

    return LoginResponse(success=False, message="Неверный логин или пароль")


# === Merchants ===


@router.get("/merchants", response_model=MerchantListResponse)
async def list_merchants(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> MerchantListResponse:
    """Получить список всех мерчантов."""
    # Count total
    count_stmt = select(func.count()).select_from(Merchant)
    total = await session.scalar(count_stmt) or 0

    # Get merchants with stats
    stmt = (
        select(
            Merchant,
            func.count(Invoice.id).label("invoices_count"),
            func.coalesce(func.sum(Invoice.amount), 0).label("total_volume"),
        )
        .outerjoin(Invoice, Merchant.id == Invoice.merchant_id)
        .options(selectinload(Merchant.api_keys))
        .group_by(Merchant.id)
        .order_by(desc(Merchant.created_at))
        .limit(limit)
        .offset(offset)
    )

    result = await session.execute(stmt)
    rows = result.all()

    items = []
    for merchant, invoices_count, total_volume in rows:
        # Get first API key preview
        api_key_preview = None
        if merchant.api_keys:
            # Получаем реальный ключ из первого ApiKey
            first_key = merchant.api_keys[0]
            # У нас в ApiKey хранится hashed_key, нам нужно взять preview из самой таблицы
            # Для простоты - просто покажем часть ID
            api_key_preview = f"{first_key.id!s}"[:8]

        items.append(
            MerchantListItem(
                id=str(merchant.id),
                name=merchant.name,
                email=merchant.email,
                is_active=merchant.is_active,
                created_at=merchant.created_at,
                api_key_preview=api_key_preview,
                invoices_count=invoices_count or 0,
                total_volume=total_volume or Decimal("0"),
            )
        )

    return MerchantListResponse(items=items, total=total)


# === System Status ===


# === System Status ===


@router.get(
    "/system-status",
    response_model=SystemStatusResponse,
    summary="Статус системы",
)
async def get_system_status(
    session: SessionDep,
) -> SystemStatusResponse:
    """
    Получить полный статус системы:
    - Статус всех сетей (checkpoint, задержка блоков, цена газа)
    - Статус воркеров
    - Баланс funder кошелька
    - Общая статистика
    """
    settings = get_settings()
    chains = get_all_chains()

    chain_statuses = []
    funder_balances = {}
    overall_healthy = True

    for chain in chains:
        config = get_chain_config(chain)
        chain_status = ChainStatus(
            chain=chain,
            chain_name=config.name,
            native_symbol=config.native_symbol,
        )

        try:
            # Получаем checkpoint
            stmt = select(ChainCheckpoint).where(ChainCheckpoint.chain == chain)
            result = await session.execute(stmt)
            checkpoint = result.scalar_one_or_none()

            if checkpoint:
                chain_status.last_scanned_block = checkpoint.last_scanned_block

            # Получаем текущий блок и цену газа
            adapter = get_evm_adapter(chain)
            latest_block = await adapter.get_latest_block_number()
            chain_status.latest_block = latest_block

            if checkpoint:
                chain_status.blocks_behind = (
                    latest_block - checkpoint.last_scanned_block
                )
                # Если отстаём больше чем на 100 блоков - проблема
                if chain_status.blocks_behind > 100:
                    chain_status.is_healthy = False
                    overall_healthy = False

            # Цена газа
            gas_price = await adapter.get_gas_price()
            if gas_price:
                chain_status.gas_price_gwei = gas_price / 10**9

            # Баланс funder
            if settings.funder_private_key.get_secret_value():
                funder_address = adapter.private_key_to_address(
                    settings.funder_private_key.get_secret_value()
                )
                balance = await adapter.get_native_balance(funder_address)
                funder_balances[chain] = float(balance)

        except Exception as e:
            chain_status.is_healthy = False
            chain_status.last_error = str(e)
            overall_healthy = False

        chain_statuses.append(chain_status)

    # Funder status
    funder_status = None
    if settings.funder_private_key.get_secret_value():
        try:
            adapter = get_evm_adapter(chains[0])
            funder_address = adapter.private_key_to_address(
                settings.funder_private_key.get_secret_value()
            )

            # Определяем сети с низким балансом
            low_balance_chains = []
            thresholds = {
                "base": 0.001,
                "arbitrum": 0.001,
                "optimism": 0.001,
                "bsc": 0.01,
                "polygon": 0.5,
                "avax": 0.1,
            }
            for chain, balance in funder_balances.items():
                if balance < thresholds.get(chain, 0.01):
                    low_balance_chains.append(chain)

            funder_status = FunderStatus(
                address=funder_address,
                balances=funder_balances,
                low_balance_chains=low_balance_chains,
            )

            if low_balance_chains:
                overall_healthy = False
        except Exception:
            pass

    # Статистика инвойсов
    total_invoices = await session.scalar(select(func.count(Invoice.id))) or 0
    pending_invoices = (
        await session.scalar(
            select(func.count(Invoice.id)).where(
                Invoice.status.in_(
                    [InvoiceStatus.AWAITING_PAYMENT, InvoiceStatus.SEEN_ONCHAIN]
                )
            )
        )
        or 0
    )

    # Завершённые за 24h
    yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
    completed_24h = (
        await session.scalar(
            select(func.count(Invoice.id)).where(
                and_(
                    Invoice.status == InvoiceStatus.CONFIRMED,
                    Invoice.updated_at >= yesterday,
                )
            )
        )
        or 0
    )

    # Статистика sweeps
    failed_sweeps = (
        await session.scalar(
            select(func.count(SweepJob.id)).where(SweepJob.state == SweepState.FAILED)
        )
        or 0
    )
    pending_sweeps = (
        await session.scalar(
            select(func.count(SweepJob.id)).where(
                SweepJob.state.in_(
                    [SweepState.PENDING_GAS, SweepState.FUNDING, SweepState.SWEEPING]
                )
            )
        )
        or 0
    )

    # Определяем общий статус
    if not overall_healthy:
        if failed_sweeps > 10 or any(not cs.is_healthy for cs in chain_statuses):
            status = "critical"
        else:
            status = "degraded"
    else:
        status = "healthy"

    return SystemStatusResponse(
        status=status,
        timestamp=datetime.now(timezone.utc),
        chains=chain_statuses,
        workers=[
            WorkerStatus(name="poller", is_running=True),
            WorkerStatus(name="sweeper", is_running=True),
            WorkerStatus(name="webhook", is_running=True),
        ],
        funder=funder_status,
        total_invoices=total_invoices,
        pending_invoices=pending_invoices,
        completed_invoices_24h=completed_24h,
        failed_sweeps=failed_sweeps,
        pending_sweeps=pending_sweeps,
    )


# === Invoices ===


@router.get(
    "/invoices",
    response_model=InvoiceListResponse,
    summary="Список инвойсов",
)
async def list_invoices(
    session: SessionDep,
    status: Annotated[str | None, Query(description="Фильтр по статусу")] = None,
    chain: Annotated[str | None, Query(description="Фильтр по сети")] = None,
    merchant_id: Annotated[str | None, Query(description="Фильтр по мерчанту")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InvoiceListResponse:
    """Получить список инвойсов с фильтрами и пагинацией."""
    stmt = select(Invoice).options(
        selectinload(Invoice.merchant),
        selectinload(Invoice.payment_sessions).selectinload(
            PaymentSession.deposit_address
        ),
        selectinload(Invoice.payment_sessions).selectinload(PaymentSession.onchain_txs),
        selectinload(Invoice.payment_sessions).selectinload(PaymentSession.sweep_job),
    )

    # Фильтры
    if status:
        try:
            status_enum = InvoiceStatus(status)
            stmt = stmt.where(Invoice.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if merchant_id:
        stmt = stmt.where(Invoice.merchant_id == merchant_id)

    if chain:
        stmt = stmt.join(PaymentSession).where(PaymentSession.chain == chain)

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    # Paginate
    offset = (page - 1) * per_page
    stmt = stmt.order_by(desc(Invoice.created_at)).limit(per_page).offset(offset)
    result = await session.execute(stmt)
    invoices = result.scalars().unique().all()

    items = []
    now = datetime.now(timezone.utc)

    for inv in invoices:
        item = InvoiceListItem(
            id=str(inv.id),
            public_id=inv.public_id,
            status=inv.status.value,
            amount=inv.amount,
            asset=inv.asset,
            created_at=inv.created_at,
            expires_at=inv.expires_at,
            is_expired=(
                now > inv.expires_at.replace(tzinfo=timezone.utc)
                if inv.expires_at.tzinfo is None
                else now > inv.expires_at
            ),
            merchant_id=str(inv.merchant_id) if inv.merchant_id else None,
            merchant_name=inv.merchant.name if inv.merchant else None,
        )

        if inv.payment_sessions:
            ps = inv.payment_sessions[0]
            item.chain = ps.chain
            item.token = ps.token
            if ps.deposit_address:
                item.deposit_address = ps.deposit_address.address
            if ps.onchain_txs:
                tx = ps.onchain_txs[0]
                item.tx_hash = tx.tx_hash
                item.confirmations = tx.confirmations
                config = get_chain_config(ps.chain)
                item.required_confirmations = config.confirmations
            if ps.sweep_job:
                item.sweep_state = ps.sweep_job.state.value

        items.append(item)

    pages = math.ceil(total / per_page) if per_page else 1

    return InvoiceListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


# === Sweeps ===


@router.get(
    "/sweeps",
    response_model=SweepListResponse,
    summary="Список sweep jobs",
)
async def list_sweeps(
    session: SessionDep,
    state: Annotated[str | None, Query(description="Фильтр по статусу")] = None,
    chain: Annotated[str | None, Query(description="Фильтр по сети")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SweepListResponse:
    """Получить список sweep jobs."""
    settings = get_settings()
    stmt = select(SweepJob).options(
        selectinload(SweepJob.payment_session).selectinload(
            PaymentSession.deposit_address
        ),
        selectinload(SweepJob.payment_session).selectinload(PaymentSession.invoice),
    )

    if state:
        try:
            state_enum = SweepState(state)
            stmt = stmt.where(SweepJob.state == state_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid state: {state}")

    if chain:
        stmt = stmt.join(PaymentSession).where(PaymentSession.chain == chain)

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    # Paginate
    offset = (page - 1) * per_page
    stmt = stmt.order_by(desc(SweepJob.created_at)).limit(per_page).offset(offset)
    result = await session.execute(stmt)
    sweeps = result.scalars().unique().all()

    items = []
    for sweep in sweeps:
        ps = sweep.payment_session
        inv = ps.invoice if ps else None

        item = SweepListItem(
            id=str(sweep.id),
            state=sweep.state.value,
            chain=ps.chain if ps else "unknown",
            token=ps.token if ps else "unknown",
            amount=inv.amount if inv else 0,
            deposit_address=(
                ps.deposit_address.address if ps and ps.deposit_address else ""
            ),
            treasury_address=settings.treasury_address,
            gas_tx_hash=sweep.gas_tx_hash,
            sweep_tx_hash=sweep.sweep_tx_hash,
            attempts=sweep.attempts,
            max_attempts=sweep.max_attempts,
            last_error=sweep.last_error,
            next_retry_at=sweep.next_retry_at,
            created_at=sweep.created_at,
            updated_at=sweep.updated_at,
            invoice_id=str(inv.id) if inv else None,
            invoice_public_id=inv.public_id if inv else None,
        )
        items.append(item)

    pages = math.ceil(total / per_page) if per_page else 1

    return SweepListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.post(
    "/sweeps/retry",
    response_model=ActionResponse,
    summary="Повторить sweep",
)
async def retry_sweep(
    request: RetrySweepRequest,
    session: SessionDep,
) -> ActionResponse:
    """Сбросить счётчик попыток и повторить sweep."""
    stmt = select(SweepJob).where(SweepJob.id == request.sweep_id)
    result = await session.execute(stmt)
    sweep = result.scalar_one_or_none()

    if not sweep:
        raise HTTPException(status_code=404, detail="Sweep not found")

    if sweep.state == SweepState.COMPLETED:
        return ActionResponse(success=False, message="Sweep already completed")

    sweep.attempts = 0
    sweep.state = SweepState.PENDING_GAS
    sweep.next_retry_at = None
    sweep.last_error = None

    await session.commit()

    return ActionResponse(
        success=True, message=f"Sweep {request.sweep_id} reset for retry"
    )


@router.post(
    "/sweeps/reset",
    response_model=ActionResponse,
    summary="Сбросить sweep",
)
async def reset_sweep(
    request: ResetSweepRequest,
    session: SessionDep,
) -> ActionResponse:
    """Сбросить sweep в указанное состояние."""
    stmt = select(SweepJob).where(SweepJob.id == request.sweep_id)
    result = await session.execute(stmt)
    sweep = result.scalar_one_or_none()

    if not sweep:
        raise HTTPException(status_code=404, detail="Sweep not found")

    sweep.attempts = 0
    sweep.state = SweepState(request.reset_to_state)
    sweep.next_retry_at = None
    sweep.last_error = None

    await session.commit()

    return ActionResponse(
        success=True,
        message=f"Sweep {request.sweep_id} reset to {request.reset_to_state}",
    )


# === Wallets ===


@router.get(
    "/wallets/balances",
    response_model=CheckAllBalancesResponse,
    summary="Проверить все балансы",
)
async def check_all_balances(
    session: SessionDep,
    with_balance_only: bool = Query(False, description="Только с балансом > 0"),
) -> CheckAllBalancesResponse:
    """
    Проверить балансы всех deposit адресов.
    Использует multicall для оптимизации RPC запросов.
    """
    from decimal import Decimal

    # Получаем все deposit адреса
    stmt = (
        select(PaymentSession)
        .options(
            selectinload(PaymentSession.deposit_address),
            selectinload(PaymentSession.invoice),
        )
        .where(PaymentSession.deposit_address_id.isnot(None))
    )

    result = await session.execute(stmt)
    payment_sessions = result.scalars().unique().all()

    balances_list: list[WalletBalanceItem] = []
    total_balances: dict[str, Decimal] = {}
    addresses_with_balance = 0
    total_addresses_checked = 0

    # Группируем по chain для batch запросов
    chain_groups: dict[str, list[PaymentSession]] = {}
    seen_addresses: set[tuple[str, str]] = set()

    for ps in payment_sessions:
        if not ps.deposit_address:
            continue

        key = (ps.deposit_address.address.lower(), ps.chain)
        if key in seen_addresses:
            continue
        seen_addresses.add(key)

        if ps.chain not in chain_groups:
            chain_groups[ps.chain] = []
        chain_groups[ps.chain].append(ps)

    # Обрабатываем каждую сеть батчем
    for chain_name, sessions in chain_groups.items():
        try:
            adapter = get_evm_adapter(chain_name)
            chain_config = get_chain_config(chain_name)

            addresses = [ps.deposit_address.address for ps in sessions]
            token_contracts = [
                chain_config.tokens["USDT"].contract_address,
                chain_config.tokens["USDC"].contract_address,
            ]

            # Batch запросы
            native_balances = await adapter.get_native_balances_batch(addresses)
            token_balances = await adapter.get_balances_batch(
                addresses, token_contracts
            )

            for ps in sessions:
                addr = ps.deposit_address.address
                addr_lower = addr.lower()
                total_addresses_checked += 1

                native_balance_ether = native_balances.get(addr_lower, Decimal(0))
                native_balance_wei = int(native_balance_ether * 10**18)
                addr_token_balances = token_balances.get(addr_lower, {})

                # Проверяем токены
                for token_symbol in ["USDT", "USDC"]:
                    token_config = chain_config.tokens.get(token_symbol)
                    if not token_config:
                        continue

                    balance = addr_token_balances.get(
                        token_config.contract_address.lower(), Decimal(0)
                    )

                    if balance > 0 or not with_balance_only:
                        balances_list.append(
                            WalletBalanceItem(
                                type="deposit_address",
                                chain=chain_name,
                                token=token_symbol,
                                address=addr,
                                balance=balance,
                                native_balance_wei=native_balance_wei,
                                invoice_id=(
                                    str(ps.invoice.id) if ps.invoice else None
                                ),
                            )
                        )

                        key = f"{chain_name}/{token_symbol}"
                        total_balances[key] = total_balances.get(key, Decimal(0)) + balance

                        if balance > 0:
                            addresses_with_balance += 1

        except Exception as e:
            # При ошибке добавляем с нулевыми балансами и логируем
            import logging
            logging.error(f"Error fetching balances for chain {chain_name}: {e}")
            
            if not with_balance_only:
                for ps in sessions:
                    total_addresses_checked += 1
                    # Добавляем запись для каждого токена с пометкой об ошибке
                    for token_symbol in ["USDT", "USDC"]:
                        balances_list.append(
                            WalletBalanceItem(
                                type="deposit_address",
                                chain=chain_name,
                                token=f"{token_symbol} (RPC error)",
                                address=ps.deposit_address.address,
                                balance=Decimal(0),
                                native_balance_wei=0,
                                invoice_id=str(ps.invoice.id) if ps.invoice else None,
                            )
                        )

    return CheckAllBalancesResponse(
        total_addresses_checked=total_addresses_checked,
        addresses_with_balance=addresses_with_balance,
        total_balances=total_balances,
        balances=balances_list,
    )


# === Logs ===


@router.get(
    "/logs",
    response_model=SystemLogsResponse,
    summary="Системные логи",
)
async def get_logs(
    session: SessionDep,
    level: Annotated[str | None, Query(description="Фильтр по уровню")] = None,
    source: Annotated[str | None, Query(description="Фильтр по источнику")] = None,
    chain: Annotated[str | None, Query(description="Фильтр по сети")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SystemLogsResponse:
    """Получить системные логи."""
    sys_logger = SystemLogger(session)

    level_enum = None
    if level:
        try:
            level_enum = SystemLogLevel(level.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid level: {level}")

    offset = (page - 1) * per_page
    logs, total = await sys_logger.get_logs(
        level=level_enum,
        source=source,
        chain=chain,
        limit=per_page,
        offset=offset,
    )

    items = [
        SystemLogEntry(
            id=str(log.id),
            timestamp=log.timestamp,
            level=log.level.value,
            source=log.source,
            chain=log.chain,
            message=log.message,
            details=log.details,
            invoice_id=str(log.invoice_id) if log.invoice_id else None,
            sweep_id=str(log.sweep_id) if log.sweep_id else None,
            tx_hash=log.tx_hash,
        )
        for log in logs
    ]

    return SystemLogsResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
    )


# === Dashboard ===


@router.get(
    "/dashboard",
    response_model=DashboardStats,
    summary="Статистика дашборда",
)
async def get_dashboard_stats(
    session: SessionDep,
) -> DashboardStats:
    """Получить статистику для дашборда."""
    now = datetime.now(timezone.utc)

    # Invoices by status
    status_counts = {}
    for status in InvoiceStatus:
        count = (
            await session.scalar(
                select(func.count(Invoice.id)).where(Invoice.status == status)
            )
            or 0
        )
        status_counts[status.value] = count

    total_invoices = sum(status_counts.values())

    # Volume
    async def get_volume(hours: int):
        cutoff = now - timedelta(hours=hours)
        result = await session.scalar(
            select(func.sum(Invoice.amount)).where(
                and_(
                    Invoice.status == InvoiceStatus.CONFIRMED,
                    Invoice.updated_at >= cutoff,
                )
            )
        )
        return result or 0

    volume_24h = await get_volume(24)
    volume_7d = await get_volume(24 * 7)
    volume_30d = await get_volume(24 * 30)

    # By chain
    chain_volume_stmt = (
        select(PaymentSession.chain, func.sum(Invoice.amount))
        .join(Invoice)
        .where(Invoice.status == InvoiceStatus.CONFIRMED)
        .group_by(PaymentSession.chain)
    )
    result = await session.execute(chain_volume_stmt)
    volume_by_chain = {row[0]: row[1] or 0 for row in result.all()}

    chain_count_stmt = (
        select(PaymentSession.chain, func.count(Invoice.id))
        .join(Invoice)
        .group_by(PaymentSession.chain)
    )
    result = await session.execute(chain_count_stmt)
    invoices_by_chain = {row[0]: row[1] for row in result.all()}

    # Sweeps
    sweep_counts = {}
    for state in SweepState:
        count = (
            await session.scalar(
                select(func.count(SweepJob.id)).where(SweepJob.state == state)
            )
            or 0
        )
        sweep_counts[state.value] = count

    total_sweeps = sum(sweep_counts.values())

    # Error counts
    sys_logger = SystemLogger(session)
    error_counts = await sys_logger.get_error_counts(hours=24)

    return DashboardStats(
        total_invoices=total_invoices,
        invoices_by_status=status_counts,
        volume_24h=volume_24h,
        volume_7d=volume_7d,
        volume_30d=volume_30d,
        volume_by_chain=volume_by_chain,
        invoices_by_chain=invoices_by_chain,
        total_sweeps=total_sweeps,
        completed_sweeps=sweep_counts.get("completed", 0),
        failed_sweeps=sweep_counts.get("failed", 0),
        pending_sweeps=(
            sweep_counts.get("pending_gas", 0)
            + sweep_counts.get("funding", 0)
            + sweep_counts.get("sweeping", 0)
        ),
        rpc_errors=error_counts.get("rpc", 0),
        sweep_errors=error_counts.get("sweeper", 0),
        webhook_errors=error_counts.get("webhook", 0),
    )


# === RPC Status ===


@router.get(
    "/rpc-status",
    summary="Статус RPC endpoints",
)
async def get_rpc_status():
    """
    Получить статус всех RPC endpoints:
    - Здоровье каждого endpoint
    - Latency
    - Error rate
    - Количество запросов
    """
    from src.blockchain.rpc_manager import _rpc_managers

    if not _rpc_managers:
        return {
            "status": "not_configured",
            "message": "RPC managers not initialized. Using single RPC per chain.",
            "chains": {},
        }

    chains_status = {}
    for chain, manager in _rpc_managers.items():
        chains_status[chain] = manager.get_stats()

    return {
        "status": "ok",
        "chains": chains_status,
    }


@router.post(
    "/rpc-health-check",
    summary="Запустить health check всех RPC",
)
async def run_rpc_health_check():
    """
    Запустить health check для всех RPC endpoints.
    Обновляет статусы и latency.
    """
    from src.blockchain.rpc_manager import _rpc_managers

    if not _rpc_managers:
        raise HTTPException(
            status_code=400,
            detail="RPC managers not configured",
        )

    results = {}
    for chain, manager in _rpc_managers.items():
        chain_results = await manager.health_check()
        results[chain] = {url: status.value for url, status in chain_results.items()}

    return {
        "status": "ok",
        "message": "Health check completed",
        "results": results,
    }


@router.get(
    "/check-all-balances",
    response_model=CheckAllBalancesResponse,
    summary="Проверить балансы всех адресов",
)
async def check_all_balances(
    session: SessionDep,
    include_zero: bool = Query(False, description="Включить адреса с нулевым балансом"),
):
    """
    Проверить балансы всех адресов в системе.

    Сканирует:
    - wallet_addresses (persistent user wallets)
    - deposit_addresses (invoice payments)

    Для каждого адреса проверяет балансы USDT и USDC.
    """
    from decimal import Decimal
    from src.db.models import WalletAddress, UserWallet, DepositAddress

    balances = []
    total_balances = {}
    checked_count = 0

    # === 1. Проверяем wallet_addresses ===
    stmt = select(WalletAddress).options(selectinload(WalletAddress.user_wallet))
    result = await session.execute(stmt)
    wallet_addrs = result.scalars().all()

    for wa in wallet_addrs:
        chain = wa.chain
        address = wa.address

        # Проверяем USDT и USDC
        for token in ["USDT", "USDC"]:
            try:
                adapter = get_evm_adapter(chain)
                chain_config = get_chain_config(chain)
                token_config = chain_config.tokens.get(token.upper())

                if not token_config:
                    continue

                balance = await adapter.get_erc20_balance(
                    address, token_config.contract_address
                )
                native_balance = await adapter.get_native_balance_wei(address)
                checked_count += 1

                key = f"{chain.upper()}/{token}"
                if key not in total_balances:
                    total_balances[key] = Decimal("0")
                total_balances[key] += balance

                if balance > 0 or include_zero:
                    user_id = (
                        wa.user_wallet.external_user_id if wa.user_wallet else None
                    )
                    balances.append(
                        WalletBalanceItem(
                            type="wallet_address",
                            chain=chain,
                            token=token,
                            address=address,
                            balance=balance,
                            native_balance_wei=native_balance,
                            user_id=user_id,
                        )
                    )
            except Exception:
                continue

    # === 2. Проверяем deposit_addresses ===
    stmt = select(PaymentSession).options(
        selectinload(PaymentSession.deposit_address),
        selectinload(PaymentSession.invoice),
    )
    result = await session.execute(stmt)
    sessions = result.scalars().all()

    for ps in sessions:
        chain = ps.chain
        token = ps.token
        address = ps.deposit_address.address

        try:
            adapter = get_evm_adapter(chain)
            chain_config = get_chain_config(chain)
            token_config = chain_config.tokens.get(token.upper())

            if not token_config:
                continue

            balance = await adapter.get_erc20_balance(
                address, token_config.contract_address
            )
            native_balance = await adapter.get_native_balance_wei(address)
            checked_count += 1

            key = f"{chain.upper()}/{token}"
            if key not in total_balances:
                total_balances[key] = Decimal("0")
            total_balances[key] += balance

            if balance > 0 or include_zero:
                balances.append(
                    WalletBalanceItem(
                        type="deposit_address",
                        chain=chain,
                        token=token,
                        address=address,
                        balance=balance,
                        native_balance_wei=native_balance,
                        invoice_id=ps.invoice.public_id,
                    )
                )
        except Exception:
            continue

    return CheckAllBalancesResponse(
        total_addresses_checked=checked_count,
        addresses_with_balance=len([b for b in balances if b.balance > 0]),
        total_balances=total_balances,
        balances=balances,
    )
