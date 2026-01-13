"""
Merchant API Router.
Эндпоинты для работы с инвойсами и webhooks.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.api.deps import IdempotencyKeyDep, MerchantDep, SessionDep
from src.core.rate_limit import limiter, RATE_LIMITS
from src.core.security import hash_api_key
from src.api.merchant.schemas import (
    ErrorResponse,
    InvoiceCreateRequest,
    InvoiceListResponse,
    InvoiceResponse,
    ManualSweepRequest,
    ManualSweepResponse,
    PaymentInfo,
    SweepJobListResponse,
    SweepJobResponse,
    TokenBalance,
    WalletBalance,
    WalletBalancesResponse,
    WebhookCreateRequest,
    WebhookListResponse,
    WebhookResponse,
)
from src.blockchain.chains import get_chain_config, get_all_chains, is_chain_supported
from src.blockchain.evm_adapter import get_evm_adapter
from src.db.models import (
    ApiKey,
    DepositAddress,
    Invoice,
    InvoiceStatus,
    Merchant,
    PaymentSession,
    UnifiedSweepJob,
    SweepSource,
    SweepState,
)
from src.services.invoice_service import InvoiceService
from src.services.webhook_service import WebhookService

router = APIRouter(tags=["Merchant API"])


# === Invoices ===


@router.post(
    "/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "Duplicate request (idempotency key)",
        },
    },
)
@limiter.limit(RATE_LIMITS["create_invoice"])
async def create_invoice(
    request: Request,
    body: InvoiceCreateRequest,
    session: SessionDep,
    merchant: MerchantDep,
    idempotency_key: IdempotencyKeyDep,
) -> InvoiceResponse:
    """
    Создать новый инвойс для оплаты.

    Используйте заголовок `Idempotency-Key` для защиты от дублирования запросов.
    """
    service = InvoiceService(session)

    invoice = await service.create_invoice(
        merchant=merchant,
        amount=body.amount,
        asset=body.asset,
        allowed_chains=body.allowed_chains,
        ttl_minutes=body.ttl_minutes,
        metadata=body.metadata,
        idempotency_key=idempotency_key,
    )

    return InvoiceResponse(
        id=invoice.id,
        public_id=invoice.public_id,
        amount=invoice.amount,
        asset=invoice.asset,
        allowed_chains=invoice.allowed_chains,
        status=invoice.status.value,
        ttl_minutes=invoice.ttl_minutes,
        expires_at=invoice.expires_at,
        metadata=invoice.extra_data,
        hosted_url=service.get_hosted_url(invoice),
        payment=None,
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Invoice not found"},
    },
)
async def get_invoice(
    invoice_id: str,
    session: SessionDep,
    merchant: MerchantDep,
) -> InvoiceResponse:
    """Получить инвойс по ID (UUID или public_id)."""
    service = InvoiceService(session)

    # Пробуем определить тип ID
    try:
        # Если это UUID
        uuid_id = UUID(invoice_id)
        invoice = await service.get_invoice(uuid_id, merchant.id)
    except ValueError:
        # Если это public_id
        invoice = await service.get_invoice_by_public_id(invoice_id)
        # Проверяем принадлежность мерчанту
        if invoice.merchant_id != merchant.id:
            from src.services.invoice_service import InvoiceNotFoundError

            raise InvoiceNotFoundError(invoice_id)

    # Формируем payment info если есть сессия
    payment = None
    if invoice.payment_sessions:
        ps = invoice.payment_sessions[0]
        chain_config = get_chain_config(ps.chain)

        # Ищем подтверждённую транзакцию
        tx = next((t for t in ps.onchain_txs if t.confirmations > 0), None)

        payment = PaymentInfo(
            chain=ps.chain,
            token=ps.token,
            deposit_address=ps.deposit_address.address if ps.deposit_address else "",
            tx_hash=tx.tx_hash if tx else None,
            confirmations=tx.confirmations if tx else 0,
            required_confirmations=chain_config.confirmations,
            confirmed_at=tx.confirmed_at if tx else None,
        )

    return InvoiceResponse(
        id=invoice.id,
        public_id=invoice.public_id,
        amount=invoice.amount,
        asset=invoice.asset,
        allowed_chains=invoice.allowed_chains,
        status=invoice.status.value,
        ttl_minutes=invoice.ttl_minutes,
        expires_at=invoice.expires_at,
        metadata=invoice.extra_data,
        hosted_url=service.get_hosted_url(invoice),
        payment=payment,
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


@router.get("/invoices", response_model=InvoiceListResponse)
async def list_invoices(
    session: SessionDep,
    merchant: MerchantDep,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[datetime] = Query(None, alias="from"),
    to_date: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> InvoiceListResponse:
    """Получить список инвойсов."""
    service = InvoiceService(session)

    # Парсим статус
    invoice_status = None
    if status_filter:
        try:
            invoice_status = InvoiceStatus(status_filter.upper())
        except ValueError:
            pass

    invoices, total = await service.list_invoices(
        merchant_id=merchant.id,
        status=invoice_status,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )

    items = []
    for invoice in invoices:
        items.append(
            InvoiceResponse(
                id=invoice.id,
                public_id=invoice.public_id,
                amount=invoice.amount,
                asset=invoice.asset,
                allowed_chains=invoice.allowed_chains,
                status=invoice.status.value,
                ttl_minutes=invoice.ttl_minutes,
                expires_at=invoice.expires_at,
                metadata=invoice.extra_data,
                hosted_url=service.get_hosted_url(invoice),
                payment=None,
                created_at=invoice.created_at,
                updated_at=invoice.updated_at,
            )
        )

    return InvoiceListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/invoices/{invoice_id}/expire",
    response_model=InvoiceResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Invoice not found"},
        400: {"model": ErrorResponse, "description": "Cannot expire invoice"},
    },
)
async def expire_invoice(
    invoice_id: UUID,
    session: SessionDep,
    merchant: MerchantDep,
) -> InvoiceResponse:
    """Принудительно завершить (expire) инвойс."""
    service = InvoiceService(session)
    invoice = await service.expire_invoice(invoice_id, merchant.id)

    return InvoiceResponse(
        id=invoice.id,
        public_id=invoice.public_id,
        amount=invoice.amount,
        asset=invoice.asset,
        allowed_chains=invoice.allowed_chains,
        status=invoice.status.value,
        ttl_minutes=invoice.ttl_minutes,
        expires_at=invoice.expires_at,
        metadata=invoice.extra_data,
        hosted_url=service.get_hosted_url(invoice),
        payment=None,
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


# === Webhooks ===


@router.post(
    "/webhooks",
    response_model=WebhookResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    request: WebhookCreateRequest,
    session: SessionDep,
    merchant: MerchantDep,
) -> WebhookResponse:
    """
    Создать новый webhook.

    Секрет webhook возвращается только при создании.
    Сохраните его для проверки подписей входящих событий.
    """
    service = WebhookService(session)

    webhook = await service.create_webhook(
        merchant=merchant,
        url=request.url,
        events=request.events,
    )

    return WebhookResponse(
        id=webhook.id,
        url=webhook.url,
        secret=webhook.secret,  # Показываем только при создании!
        events=webhook.events,
        is_active=webhook.is_active,
        created_at=webhook.created_at,
    )


@router.get("/webhooks", response_model=WebhookListResponse)
async def list_webhooks(
    session: SessionDep,
    merchant: MerchantDep,
) -> WebhookListResponse:
    """Получить список webhooks."""
    service = WebhookService(session)
    webhooks = await service.list_webhooks(merchant.id)

    items = [
        WebhookResponse(
            id=w.id,
            url=w.url,
            secret="***",  # Скрываем секрет в списке
            events=w.events,
            is_active=w.is_active,
            created_at=w.created_at,
        )
        for w in webhooks
    ]

    return WebhookListResponse(items=items)


@router.delete(
    "/webhooks/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ErrorResponse, "description": "Webhook not found"},
    },
)
async def delete_webhook(
    webhook_id: UUID,
    session: SessionDep,
    merchant: MerchantDep,
) -> None:
    """Удалить webhook."""
    service = WebhookService(session)
    await service.delete_webhook(webhook_id, merchant.id)


# === Sweep Jobs ===


@router.get("/sweeps", response_model=SweepJobListResponse)
async def list_sweep_jobs(
    session: SessionDep,
    merchant: MerchantDep,
    state_filter: Optional[str] = Query(None, alias="state"),
    limit: int = Query(50, ge=1, le=100),
) -> SweepJobListResponse:
    """Получить список задач на вывод (sweep jobs) из UnifiedSweepJob."""
    from sqlalchemy import and_

    # Строим запрос для UnifiedSweepJob, фильтруя по source='invoice'
    # и проверяя что source_id связан с invoice мерчанта
    stmt = (
        select(UnifiedSweepJob)
        .where(UnifiedSweepJob.source == SweepSource.INVOICE)
        .order_by(UnifiedSweepJob.created_at.desc())
        .limit(limit)
    )

    # Фильтр по статусу
    if state_filter:
        try:
            state = SweepState(state_filter.lower())
            stmt = stmt.where(UnifiedSweepJob.state == state)
        except ValueError:
            pass

    result = await session.execute(stmt)
    sweep_jobs = result.scalars().all()

    items = []
    for sweep in sweep_jobs:
        # Получаем связанную payment_session
        ps_stmt = select(PaymentSession).where(PaymentSession.id == sweep.source_id)
        ps_result = await session.execute(ps_stmt)
        ps = ps_result.scalar_one_or_none()
        
        if not ps:
            continue
            
        # Получаем invoice
        inv_stmt = select(Invoice).where(Invoice.id == ps.invoice_id)
        inv_result = await session.execute(inv_stmt)
        invoice = inv_result.scalar_one_or_none()
        
        if not invoice or invoice.merchant_id != merchant.id:
            continue

        items.append(
            SweepJobResponse(
                id=sweep.id,
                invoice_id=invoice.id,
                invoice_public_id=invoice.public_id,
                chain=sweep.chain,
                token=sweep.token,
                deposit_address=sweep.from_address,
                amount=sweep.amount,
                state=sweep.state.value,
                gas_tx_hash=sweep.gas_tx_hash,
                sweep_tx_hash=sweep.sweep_tx_hash,
                attempts=sweep.attempts,
                last_error=sweep.last_error,
                created_at=sweep.created_at,
            )
        )

    return SweepJobListResponse(items=items, total=len(items))


# === Wallet Balances ===


@router.get("/wallets/balances", response_model=WalletBalancesResponse)
async def get_wallet_balances(
    session: SessionDep,
    merchant: MerchantDep,
    chain_filter: Optional[str] = Query(
        None, alias="chain", description="Фильтр по сети"
    ),
    with_balance_only: bool = Query(False, description="Только с балансом > 0"),
) -> WalletBalancesResponse:
    """
    Получить балансы всех deposit кошельков мерчанта.

    Использует Multicall для батчинга RPC вызовов (1 вызов вместо N×3).

    Проверяет:
    - Native баланс (ETH/BNB)
    - USDT баланс
    - USDC баланс
    """
    from decimal import Decimal

    # Получаем все payment sessions мерчанта с deposit адресами
    stmt = (
        select(PaymentSession)
        .join(Invoice, Invoice.id == PaymentSession.invoice_id)
        .options(
            selectinload(PaymentSession.deposit_address),
            selectinload(PaymentSession.invoice),
        )
        .where(Invoice.merchant_id == merchant.id)
    )

    if chain_filter:
        stmt = stmt.where(PaymentSession.chain == chain_filter.lower())

    result = await session.execute(stmt)
    payment_sessions = result.scalars().all()

    items: list[WalletBalance] = []
    total_usdt = Decimal("0")
    total_usdc = Decimal("0")

    # Группируем по chain для батч-запросов
    chain_groups: dict[str, list[tuple[str, PaymentSession]]] = {}
    seen_addresses: set[tuple[str, str]] = set()

    for ps in payment_sessions:
        if not ps.deposit_address:
            continue

        key = (ps.deposit_address.address.lower(), ps.chain)
        if key in seen_addresses:
            continue
        seen_addresses.add(key)

        chain = ps.chain
        if chain not in chain_groups:
            chain_groups[chain] = []
        chain_groups[chain].append((ps.deposit_address.address, ps))

    # Обрабатываем каждую сеть батчем
    for chain_name, addresses_and_sessions in chain_groups.items():
        try:
            adapter = get_evm_adapter(chain_name)
            chain_config = get_chain_config(chain_name)

            addresses = [addr for addr, _ in addresses_and_sessions]
            token_contracts = [
                chain_config.tokens["USDT"].contract_address,
                chain_config.tokens["USDC"].contract_address,
            ]

            # ОПТИМИЗАЦИЯ: Батч-запрос всех балансов за 2 RPC вызова вместо N×3
            native_balances = await adapter.get_native_balances_batch(addresses)
            token_balances = await adapter.get_balances_batch(
                addresses, token_contracts
            )

            # Формируем результаты
            for address, ps in addresses_and_sessions:
                addr_lower = address.lower()
                deposit = ps.deposit_address
                invoice = ps.invoice

                native_balance = native_balances.get(addr_lower, Decimal(0))

                tokens: list[TokenBalance] = []
                addr_token_balances = token_balances.get(addr_lower, {})

                for token_symbol in ["USDT", "USDC"]:
                    token_config = chain_config.tokens.get(token_symbol)
                    if token_config:
                        balance = addr_token_balances.get(
                            token_config.contract_address.lower(), Decimal(0)
                        )
                        if balance > 0 or not with_balance_only:
                            tokens.append(
                                TokenBalance(
                                    token=token_symbol,
                                    balance=balance,
                                    contract=token_config.contract_address,
                                )
                            )
                            if token_symbol == "USDT":
                                total_usdt += balance
                            else:
                                total_usdc += balance

                # Пропускаем если нужны только с балансом
                if (
                    with_balance_only
                    and native_balance == 0
                    and not any(t.balance > 0 for t in tokens)
                ):
                    continue

                items.append(
                    WalletBalance(
                        address=deposit.address,
                        chain=chain_name,
                        native_balance=native_balance,
                        native_symbol=chain_config.native_symbol,
                        tokens=tokens,
                        invoice_public_id=invoice.public_id if invoice else None,
                        invoice_status=invoice.status.value if invoice else None,
                    )
                )

        except Exception as e:
            # Добавляем с нулевыми балансами при ошибке
            if not with_balance_only:
                for address, ps in addresses_and_sessions:
                    try:
                        chain_config = get_chain_config(chain_name)
                        items.append(
                            WalletBalance(
                                address=ps.deposit_address.address,
                                chain=chain_name,
                                native_balance=Decimal("0"),
                                native_symbol=chain_config.native_symbol,
                                tokens=[],
                                invoice_public_id=(
                                    ps.invoice.public_id if ps.invoice else None
                                ),
                                invoice_status=(
                                    ps.invoice.status.value if ps.invoice else None
                                ),
                            )
                        )
                    except Exception:
                        pass

    return WalletBalancesResponse(
        items=items,
        total=len(items),
        total_usdt=total_usdt,
        total_usdc=total_usdc,
    )


@router.post("/wallets/sweep", response_model=ManualSweepResponse)
async def manual_sweep(
    request: ManualSweepRequest,
    session: SessionDep,
    merchant: MerchantDep,
) -> ManualSweepResponse:
    """
    Запустить ручной sweep для указанного адреса.

    Создаёт SweepJob для перевода токенов на treasury.
    """
    from decimal import Decimal

    # Находим payment session для этого адреса и сети
    stmt = (
        select(PaymentSession)
        .join(DepositAddress, PaymentSession.deposit_address_id == DepositAddress.id)
        .join(Invoice, Invoice.id == PaymentSession.invoice_id)
        .where(
            DepositAddress.address.ilike(request.address),
            PaymentSession.chain == request.chain.lower(),
            Invoice.merchant_id == merchant.id,
        )
        .options(
            selectinload(PaymentSession.deposit_address),
            selectinload(PaymentSession.invoice),
        )
    )

    result = await session.execute(stmt)
    payment_session = result.scalar_one_or_none()

    if not payment_session:
        return ManualSweepResponse(
            status="error",
            message=f"Deposit address {request.address} not found for chain {request.chain}",
        )

    deposit = payment_session.deposit_address

    # Проверяем баланс
    try:
        adapter = get_evm_adapter(request.chain.lower())
        chain_config = get_chain_config(request.chain.lower())
        token_config = chain_config.tokens.get(request.token.upper())

        if not token_config:
            return ManualSweepResponse(
                status="error",
                message=f"Token {request.token} not supported on {request.chain}",
            )

        balance = await adapter.get_erc20_balance(
            deposit.address, token_config.contract_address
        )

        if balance <= 0:
            return ManualSweepResponse(
                status="error",
                message=f"No {request.token} balance on {deposit.address}",
                balance=Decimal("0"),
            )

    except Exception as e:
        return ManualSweepResponse(
            status="error",
            message=f"Failed to check balance: {str(e)}",
        )

    # Проверяем существующий sweep job в unified_sweep_jobs
    existing_stmt = select(UnifiedSweepJob).where(
        and_(
            UnifiedSweepJob.source == SweepSource.INVOICE,
            UnifiedSweepJob.source_id == payment_session.id,
        )
    )
    existing_result = await session.execute(existing_stmt)
    existing_job = existing_result.scalar_one_or_none()

    if existing_job:
        # Если job активен - вернуть его
        if existing_job.state in [
            SweepState.PENDING_GAS,
            SweepState.FUNDING,
            SweepState.SWEEPING,
        ]:
            return ManualSweepResponse(
                sweep_job_id=existing_job.id,
                status="exists",
                message=f"Sweep job already exists with state: {existing_job.state.value}",
                balance=balance,
            )

        # Если job failed или completed - сбросить и перезапустить
        existing_job.state = SweepState.PENDING_GAS
        existing_job.attempts = 0
        existing_job.last_error = None
        existing_job.gas_tx_hash = None
        existing_job.sweep_tx_hash = None
        existing_job.next_retry_at = None
        await session.commit()
        await session.refresh(existing_job)

        return ManualSweepResponse(
            sweep_job_id=existing_job.id,
            status="created",
            message=f"Sweep job restarted for {balance} {request.token}",
            balance=balance,
        )

    # Создаём новый UnifiedSweepJob
    config = get_chain_config(request.chain)
    token_config = config.tokens.get(request.token)
    decimals = token_config.decimals if token_config else 6
    
    from src.core.config import get_settings
    settings = get_settings()
    
    sweep_job = UnifiedSweepJob(
        source=SweepSource.INVOICE,
        source_id=payment_session.id,
        chain=request.chain,
        token=request.token,
        token_contract=token_config.contract_address if token_config else "",
        from_address=deposit.address,
        to_address=settings.get_treasury_address(request.chain),
        encrypted_private_key=deposit.encrypted_privkey.hex() if isinstance(deposit.encrypted_privkey, bytes) else deposit.encrypted_privkey,
        amount=balance,
        amount_raw=str(int(balance * (10 ** decimals))),
        state=SweepState.PENDING_GAS,
        attempts=0,
        max_attempts=5,
        priority=50 if balance >= 100 else 10,
    )
    session.add(sweep_job)
    await session.commit()
    await session.refresh(sweep_job)

    return ManualSweepResponse(
        sweep_job_id=sweep_job.id,
        status="created",
        message=f"Sweep job created for {balance} {request.token}",
        balance=balance,
    )


# === Merchant Dashboard ===


async def get_merchant_by_api_key(session, api_key: str) -> Merchant | None:
    """Получить мерчанта по API ключу."""
    key_hash = hash_api_key(api_key)

    stmt = (
        select(ApiKey)
        .where(ApiKey.key_hash == key_hash)
        .where(ApiKey.is_active == True)  # noqa: E712
    )
    result = await session.execute(stmt)
    api_key_record = result.scalar_one_or_none()

    if api_key_record is None:
        return None

    stmt = (
        select(Merchant)
        .where(Merchant.id == api_key_record.merchant_id)
        .where(Merchant.is_active == True)  # noqa: E712
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
