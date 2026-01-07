"""
Merchant API Router.
Эндпоинты для работы с инвойсами и webhooks.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
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
from src.blockchain.chains import CHAINS_CONFIG, get_chain_config
from src.blockchain.evm_adapter import get_evm_adapter
from src.db.models import (
    ApiKey,
    DepositAddress,
    Invoice,
    InvoiceStatus,
    Merchant,
    PaymentSession,
    SweepJob,
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
    """Получить список задач на вывод (sweep jobs)."""

    # Строим запрос с join через payment_session -> invoice
    stmt = (
        select(SweepJob)
        .options(
            selectinload(SweepJob.payment_session).selectinload(PaymentSession.invoice),
            selectinload(SweepJob.payment_session).selectinload(
                PaymentSession.deposit_address
            ),
        )
        .join(SweepJob.payment_session)
        .join(PaymentSession.invoice)
        .where(Invoice.merchant_id == merchant.id)
        .order_by(SweepJob.created_at.desc())
        .limit(limit)
    )

    # Фильтр по статусу
    if state_filter:
        try:
            state = SweepState(state_filter.lower())
            stmt = stmt.where(SweepJob.state == state)
        except ValueError:
            pass

    result = await session.execute(stmt)
    sweep_jobs = result.scalars().all()

    items = []
    for sweep in sweep_jobs:
        ps = sweep.payment_session
        invoice = ps.invoice
        deposit = ps.deposit_address

        items.append(
            SweepJobResponse(
                id=sweep.id,
                invoice_id=invoice.id,
                invoice_public_id=invoice.public_id,
                chain=ps.chain,
                token=ps.token,
                deposit_address=deposit.address,
                amount=invoice.amount,
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

    # Проверяем существующий sweep job
    existing_stmt = select(SweepJob).where(
        SweepJob.payment_session_id == payment_session.id
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

    # Создаём новый sweep job
    sweep_job = SweepJob(
        payment_session_id=payment_session.id,
        state=SweepState.PENDING_GAS,
        attempts=0,
        max_attempts=5,
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
    Отображает балансы, инвойсы и форму создания нового инвойса.

    Авторизация через query параметр api_key.
    """
    if not api_key:
        return HTMLResponse(content=_render_login_page())

    merchant = await get_merchant_by_api_key(session, api_key)
    if merchant is None:
        return HTMLResponse(
            content=_render_login_page("Неверный API ключ"),
            status_code=401,
        )
    return HTMLResponse(content=_render_merchant_dashboard(merchant, api_key))


def _render_login_page(error: str = "") -> str:
    """Рендер страницы входа."""
    return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Merchant Dashboard - Вход</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .login-card {{
            background: white;
            border-radius: 16px;
            padding: 40px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }}
        h1 {{
            text-align: center;
            margin-bottom: 8px;
            color: #1e293b;
        }}
        .subtitle {{
            text-align: center;
            color: #64748b;
            margin-bottom: 32px;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
            color: #374151;
        }}
        input {{
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e5e7eb;
            border-radius: 8px;
            font-size: 16px;
            transition: border-color 0.2s;
        }}
        input:focus {{
            outline: none;
            border-color: #6366f1;
        }}
        .error {{
            background: #fee2e2;
            color: #991b1b;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }}
        .btn {{
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(99, 102, 241, 0.3);
        }}
    </style>
</head>
<body>
    <div class="login-card">
        <h1>Merchant Dashboard</h1>
        <p class="subtitle">Введите ваш API ключ для входа</p>
        {'<div class="error">' + error + '</div>' if error else ''}
        <form method="GET" action="/v1/dashboard">
            <div class="form-group">
                <label for="api_key">API Key</label>
                <input type="text" id="api_key" name="api_key" placeholder="Введите API ключ" required>
            </div>
            <button type="submit" class="btn">Войти</button>
        </form>
    </div>
</body>
</html>
    """


def _render_merchant_dashboard(merchant, api_key: str) -> str:
    """Рендер HTML страницы merchant dashboard."""
    chains_config = []
    for chain_name, config in CHAINS_CONFIG.items():
        chains_config.append(
            {
                "id": chain_name,
                "name": config.name,
                "symbol": config.native_symbol,
            }
        )

    return f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Merchant Dashboard - {merchant.name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        :root {{
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #3b82f6;
            --bg: #f8fafc;
            --card: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }}

        .header {{
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            padding: 24px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}

        .header h1 {{
            font-size: 24px;
            font-weight: 700;
        }}

        .header .merchant-name {{
            opacity: 0.9;
            font-size: 14px;
            margin-top: 4px;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }}

        .grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
        }}

        @media (max-width: 1024px) {{
            .grid {{ grid-template-columns: 1fr; }}
        }}

        .card {{
            background: var(--card);
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }}

        .card-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .card-header h2 {{
            font-size: 16px;
            font-weight: 600;
        }}

        .card-body {{
            padding: 20px;
        }}

        /* Balances */
        .balance-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 16px;
        }}

        .balance-item {{
            background: var(--bg);
            border-radius: 8px;
            padding: 16px;
        }}

        .balance-item .chain {{
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .balance-item .amount {{
            font-size: 24px;
            font-weight: 700;
            margin: 4px 0;
        }}

        .balance-item .token {{
            font-size: 14px;
            color: var(--primary);
            font-weight: 600;
        }}

        .total-balance {{
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 16px;
        }}

        .total-balance .label {{
            font-size: 14px;
            opacity: 0.9;
        }}

        .total-balance .value {{
            font-size: 32px;
            font-weight: 700;
        }}

        /* Form */
        .form-group {{
            margin-bottom: 16px;
        }}

        .form-group label {{
            display: block;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 6px;
            color: var(--text);
        }}

        .form-group input,
        .form-group select {{
            width: 100%;
            padding: 10px 14px;
            border: 1px solid var(--border);
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.2s, box-shadow 0.2s;
        }}

        .form-group input:focus,
        .form-group select:focus {{
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }}

        .checkbox-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
        }}

        .checkbox-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            background: var(--bg);
            border-radius: 6px;
            cursor: pointer;
            transition: background 0.2s;
        }}

        .checkbox-item:hover {{
            background: var(--border);
        }}

        .checkbox-item input {{
            width: auto;
        }}

        .checkbox-item.selected {{
            background: var(--primary);
            color: white;
        }}

        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .btn-primary {{
            background: var(--primary);
            color: white;
        }}

        .btn-primary:hover {{
            background: var(--primary-dark);
            transform: translateY(-1px);
        }}

        .btn-primary:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }}

        .btn-sm {{
            padding: 6px 12px;
            font-size: 12px;
        }}

        .btn-success {{
            background: var(--success);
            color: white;
        }}

        /* Invoice Table */
        .invoice-table {{
            width: 100%;
            border-collapse: collapse;
        }}

        .invoice-table th,
        .invoice-table td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }}

        .invoice-table th {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            font-weight: 600;
        }}

        .invoice-table tr:hover {{
            background: var(--bg);
        }}

        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }}

        .badge-created {{ background: #fef3c7; color: #92400e; }}
        .badge-awaiting_payment {{ background: #fef3c7; color: #92400e; }}
        .badge-seen_onchain {{ background: #dbeafe; color: #1e40af; }}
        .badge-confirmed {{ background: #d1fae5; color: #065f46; }}
        .badge-expired {{ background: #fee2e2; color: #991b1b; }}

        .mono {{
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 13px;
        }}

        .text-muted {{
            color: var(--text-muted);
        }}

        /* Toast */
        .toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            padding: 16px 24px;
            background: var(--success);
            color: white;
            border-radius: 8px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s;
            z-index: 1000;
        }}

        .toast.show {{
            transform: translateY(0);
            opacity: 1;
        }}

        .toast.error {{
            background: var(--danger);
        }}

        /* Loading */
        .loading {{
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255,255,255,0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        .empty-state {{
            text-align: center;
            padding: 40px;
            color: var(--text-muted);
        }}

        .empty-state svg {{
            width: 48px;
            height: 48px;
            opacity: 0.5;
            margin-bottom: 16px;
        }}

        /* Modal */
        .modal-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            opacity: 0;
            visibility: hidden;
            transition: all 0.3s;
        }}

        .modal-overlay.show {{
            opacity: 1;
            visibility: visible;
        }}

        .modal {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 500px;
            width: 90%;
            transform: scale(0.9);
            transition: transform 0.3s;
        }}

        .modal-overlay.show .modal {{
            transform: scale(1);
        }}

        .modal h3 {{
            margin-bottom: 16px;
        }}

        .modal-actions {{
            display: flex;
            gap: 12px;
            justify-content: flex-end;
            margin-top: 24px;
        }}

        .copy-link {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--bg);
            padding: 12px;
            border-radius: 8px;
            margin-top: 12px;
        }}

        .copy-link input {{
            flex: 1;
            border: none;
            background: transparent;
            font-size: 14px;
        }}

        .refresh-btn {{
            background: transparent;
            border: none;
            cursor: pointer;
            padding: 8px;
            border-radius: 6px;
            transition: background 0.2s;
        }}

        .refresh-btn:hover {{
            background: var(--border);
        }}

        /* Navigation Tabs */
        .nav-tabs {{
            display: flex;
            gap: 4px;
            background: var(--card);
            padding: 8px;
            border-radius: 12px;
            margin-bottom: 24px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}

        .nav-tab {{
            padding: 12px 24px;
            border: none;
            background: transparent;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-muted);
            transition: all 0.2s;
        }}

        .nav-tab:hover {{
            background: var(--bg);
            color: var(--text);
        }}

        .nav-tab.active {{
            background: var(--primary);
            color: white;
        }}

        .tab-content {{
            display: none;
        }}

        .tab-content.active {{
            display: block;
        }}

        /* API Docs */
        .endpoint-card {{
            background: var(--bg);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
        }}

        .endpoint-method {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 700;
            margin-right: 8px;
        }}

        .endpoint-method.get {{ background: #10b981; color: white; }}
        .endpoint-method.post {{ background: #3b82f6; color: white; }}
        .endpoint-method.put {{ background: #f59e0b; color: white; }}
        .endpoint-method.delete {{ background: #ef4444; color: white; }}

        .endpoint-path {{
            font-family: monospace;
            font-size: 14px;
            font-weight: 600;
        }}

        .endpoint-desc {{
            color: var(--text-muted);
            font-size: 13px;
            margin-top: 8px;
        }}

        .code-block {{
            background: #1e293b;
            color: #e2e8f0;
            border-radius: 8px;
            padding: 16px;
            font-family: 'Fira Code', monospace;
            font-size: 13px;
            overflow-x: auto;
            margin: 12px 0;
        }}

        .code-block .keyword {{ color: #c084fc; }}
        .code-block .string {{ color: #86efac; }}
        .code-block .number {{ color: #fcd34d; }}
        .code-block .comment {{ color: #64748b; }}

        .webhook-example {{
            border-left: 3px solid var(--primary);
            padding-left: 16px;
            margin: 16px 0;
        }}

        .section-title {{
            font-size: 18px;
            font-weight: 600;
            margin: 24px 0 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border);
        }}

        .section-title:first-child {{
            margin-top: 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Merchant Dashboard</h1>
        <div class="merchant-name">{merchant.name}</div>
    </div>

    <div class="container">
        <!-- Navigation Tabs -->
        <div class="nav-tabs">
            <button class="nav-tab active" onclick="showTab('main')">Главная</button>
            <button class="nav-tab" onclick="showTab('balance')">Баланс</button>
            <button class="nav-tab" onclick="showTab('api')">API</button>
            <button class="nav-tab" onclick="showTab('webhooks')">Webhooks</button>
        </div>

        <!-- Main Tab -->
        <div id="tab-main" class="tab-content active">
        <div class="grid">
            <!-- Create Invoice -->
            <div class="card">
                <div class="card-header">
                    <h2>Создать инвойс</h2>
                </div>
                <div class="card-body">
                    <form id="createInvoiceForm">
                        <div class="form-group">
                            <label>Сумма</label>
                            <input type="number" id="amount" step="0.01" min="0.01" placeholder="100.00" required>
                        </div>

                        <div class="form-group">
                            <label>Токен</label>
                            <select id="asset">
                                <option value="USDT">USDT</option>
                                <option value="USDC">USDC</option>
                            </select>
                        </div>

                        <div class="form-group">
                            <label>Разрешённые сети</label>
                            <div class="checkbox-group" id="chainsGroup">
                                {"".join([f'''
                                <label class="checkbox-item selected" data-chain="{c['id']}">
                                    <input type="checkbox" name="chains" value="{c['id']}" checked>
                                    {c['name']}
                                </label>
                                ''' for c in chains_config])}
                            </div>
                        </div>

                        <div class="form-group">
                            <label>Время жизни (минут)</label>
                            <select id="ttl">
                                <option value="15">15 минут</option>
                                <option value="30">30 минут</option>
                                <option value="60" selected>1 час</option>
                                <option value="180">3 часа</option>
                                <option value="720">12 часов</option>
                                <option value="1440">24 часа</option>
                            </select>
                        </div>

                        <button type="submit" class="btn btn-primary" style="width: 100%;">
                            Создать инвойс
                        </button>
                    </form>
                </div>
            </div>

            <!-- Balances -->
            <div class="card">
                <div class="card-header">
                    <h2>Балансы</h2>
                    <button class="refresh-btn" onclick="loadBalances()" title="Обновить">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M23 4v6h-6M1 20v-6h6"/>
                            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
                        </svg>
                    </button>
                </div>
                <div class="card-body">
                    <div class="total-balance">
                        <div class="label">Общий баланс</div>
                        <div class="value" id="totalBalance">$0.00</div>
                    </div>
                    <div class="balance-grid" id="balancesGrid">
                        <div class="empty-state">Загрузка...</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Invoices -->
        <div class="card" style="margin-top: 24px;">
            <div class="card-header">
                <h2>Инвойсы</h2>
                <button class="refresh-btn" onclick="loadInvoices()" title="Обновить">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M23 4v6h-6M1 20v-6h6"/>
                        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
                    </svg>
                </button>
            </div>
            <div class="card-body" style="padding: 0;">
                <table class="invoice-table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Сумма</th>
                            <th>Статус</th>
                            <th>Сеть</th>
                            <th>Создан</th>
                            <th>Действия</th>
                        </tr>
                    </thead>
                    <tbody id="invoicesTable">
                        <tr>
                            <td colspan="6" class="empty-state">Загрузка...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
        </div> <!-- End Main Tab -->

        <!-- Balance Tab -->
        <div id="tab-balance" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <h2>Детальный баланс по сетям</h2>
                    <button class="refresh-btn" onclick="loadDetailedBalances()" title="Обновить">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M23 4v6h-6M1 20v-6h6"/>
                            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
                        </svg>
                    </button>
                </div>
                <div class="card-body">
                    <div class="total-balance">
                        <div class="label">Общий баланс</div>
                        <div class="value" id="detailedTotalBalance">$0.00</div>
                    </div>
                    <div id="detailedBalances">
                        <div class="empty-state">Загрузка...</div>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-top: 24px;">
                <div class="card-header">
                    <h2>Sweep Jobs</h2>
                </div>
                <div class="card-body" style="padding: 0;">
                    <table class="invoice-table">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Сеть</th>
                                <th>Сумма</th>
                                <th>Статус</th>
                                <th>Создан</th>
                            </tr>
                        </thead>
                        <tbody id="sweepJobsTable">
                            <tr>
                                <td colspan="5" class="empty-state">Загрузка...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- API Tab -->
        <div id="tab-api" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <h2>API Документация</h2>
                </div>
                <div class="card-body">
                    <div class="section-title">Аутентификация</div>
                    <p style="margin-bottom: 12px;">Все API запросы должны содержать заголовок:</p>
                    <div class="code-block">
Authorization: Bearer YOUR_API_KEY
                    </div>
                    <p style="color: var(--text-muted); font-size: 13px;">
                        Ваш API ключ: <code style="background: var(--bg); padding: 2px 6px; border-radius: 4px;">{api_key[:8]}...{api_key[-4:]}</code>
                    </p>

                    <div class="section-title">Endpoints</div>

                    <div class="endpoint-card">
                        <span class="endpoint-method post">POST</span>
                        <span class="endpoint-path">/v1/invoices</span>
                        <div class="endpoint-desc">Создать новый инвойс для оплаты</div>
                        <div class="code-block">
<span class="comment">// Request</span>
{{
  "<span class="string">amount</span>": "<span class="number">100.00</span>",
  "<span class="string">asset</span>": "<span class="string">USDT</span>",
  "<span class="string">allowed_chains</span>": ["<span class="string">base</span>", "<span class="string">arbitrum</span>"],
  "<span class="string">ttl_minutes</span>": <span class="number">60</span>,
  "<span class="string">metadata</span>": {{"<span class="string">order_id</span>": "<span class="string">12345</span>"}}
}}

<span class="comment">// Response</span>
{{
  "<span class="string">id</span>": "<span class="string">uuid</span>",
  "<span class="string">public_id</span>": "<span class="string">INV-XXXXXX</span>",
  "<span class="string">hosted_url</span>": "<span class="string">https://...</span>",
  "<span class="string">status</span>": "<span class="string">created</span>",
  "<span class="string">expires_at</span>": "<span class="string">2025-01-01T12:00:00Z</span>"
}}
                        </div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method get">GET</span>
                        <span class="endpoint-path">/v1/invoices</span>
                        <div class="endpoint-desc">Получить список инвойсов с пагинацией</div>
                        <div class="code-block">
<span class="comment">// Query params: ?limit=50&offset=0&status=confirmed</span>

<span class="comment">// Response</span>
{{
  "<span class="string">items</span>": [...],
  "<span class="string">total</span>": <span class="number">100</span>,
  "<span class="string">limit</span>": <span class="number">50</span>,
  "<span class="string">offset</span>": <span class="number">0</span>
}}
                        </div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method get">GET</span>
                        <span class="endpoint-path">/v1/invoices/{{public_id}}</span>
                        <div class="endpoint-desc">Получить детали инвойса по ID</div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method get">GET</span>
                        <span class="endpoint-path">/v1/wallets/balances</span>
                        <div class="endpoint-desc">Получить балансы всех депозитных кошельков</div>
                        <div class="code-block">
<span class="comment">// Query params: ?with_balance_only=true</span>

<span class="comment">// Response</span>
{{
  "<span class="string">items</span>": [
    {{
      "<span class="string">address</span>": "<span class="string">0x...</span>",
      "<span class="string">chain</span>": "<span class="string">base</span>",
      "<span class="string">native_balance</span>": "<span class="number">0.001</span>",
      "<span class="string">tokens</span>": [
        {{"<span class="string">token</span>": "<span class="string">USDT</span>", "<span class="string">balance</span>": "<span class="number">100.50</span>"}}
      ]
    }}
  ],
  "<span class="string">total_usdt</span>": "<span class="number">500.00</span>",
  "<span class="string">total_usdc</span>": "<span class="number">300.00</span>"
}}
                        </div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method post">POST</span>
                        <span class="endpoint-path">/v1/webhooks</span>
                        <div class="endpoint-desc">Создать webhook endpoint</div>
                        <div class="code-block">
<span class="comment">// Request</span>
{{
  "<span class="string">url</span>": "<span class="string">https://your-server.com/webhook</span>",
  "<span class="string">events</span>": ["<span class="string">invoice.confirmed</span>", "<span class="string">invoice.expired</span>"]
}}
                        </div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method get">GET</span>
                        <span class="endpoint-path">/v1/webhooks</span>
                        <div class="endpoint-desc">Получить список настроенных webhooks</div>
                    </div>

                    <div class="endpoint-card">
                        <span class="endpoint-method get">GET</span>
                        <span class="endpoint-path">/v1/sweep-jobs</span>
                        <div class="endpoint-desc">Получить статус sweep операций</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Webhooks Tab -->
        <div id="tab-webhooks" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <h2>Webhook Events</h2>
                </div>
                <div class="card-body">
                    <p style="margin-bottom: 16px;">
                        Webhooks отправляются на ваш сервер при изменении статуса инвойса.
                        Каждый запрос подписывается HMAC-SHA256.
                    </p>

                    <div class="section-title">Доступные события</div>
                    <ul style="margin-left: 20px; color: var(--text-muted);">
                        <li><code>invoice.created</code> — Инвойс создан</li>
                        <li><code>invoice.seen_onchain</code> — Обнаружена транзакция (ожидает подтверждений)</li>
                        <li><code>invoice.confirmed</code> — Платёж подтверждён</li>
                        <li><code>invoice.expired</code> — Инвойс истёк</li>
                    </ul>

                    <div class="section-title">Формат запроса</div>
                    <div class="code-block">
POST /your-webhook-endpoint HTTP/1.1
Content-Type: application/json
X-Webhook-Signature: sha256=abc123...
X-Webhook-Timestamp: 1704067200
X-Webhook-Event: invoice.confirmed

{{
  "<span class="string">invoice_id</span>": "<span class="string">INV-ABC123</span>",
  "<span class="string">merchant_order_id</span>": "<span class="string">order-456</span>",
  "<span class="string">status</span>": "<span class="string">confirmed</span>",
  "<span class="string">amount</span>": "<span class="string">100.00</span>",
  "<span class="string">asset</span>": "<span class="string">USDT</span>",
  "<span class="string">chain</span>": "<span class="string">base</span>",
  "<span class="string">tx_hash</span>": "<span class="string">0x...</span>",
  "<span class="string">confirmed_at</span>": "<span class="string">2025-01-01T12:00:00Z</span>"
}}
                    </div>

                    <div class="section-title">Проверка подписи (Python)</div>
                    <div class="code-block">
<span class="keyword">import</span> hmac
<span class="keyword">import</span> hashlib
<span class="keyword">import</span> time

<span class="keyword">def</span> <span class="string">verify_webhook</span>(payload: bytes, signature: str, timestamp: str, secret: str) -> bool:
    <span class="comment"># Проверка timestamp (не старше 5 минут)</span>
    <span class="keyword">if</span> abs(time.time() - int(timestamp)) > 300:
        <span class="keyword">return</span> False

    <span class="comment"># Вычисляем подпись</span>
    message = timestamp.encode() + b'.' + payload
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    <span class="comment"># Сравниваем безопасно</span>
    <span class="keyword">return</span> hmac.compare_digest(f"sha256={{expected}}", signature)
                    </div>

                    <div class="section-title">Проверка подписи (Node.js)</div>
                    <div class="code-block">
<span class="keyword">const</span> crypto = require('<span class="string">crypto</span>');

<span class="keyword">function</span> <span class="string">verifyWebhook</span>(payload, signature, timestamp, secret) {{
    <span class="comment">// Проверка timestamp (не старше 5 минут)</span>
    <span class="keyword">if</span> (Math.abs(Date.now()/1000 - parseInt(timestamp)) > 300) {{
        <span class="keyword">return</span> false;
    }}

    <span class="comment">// Вычисляем подпись</span>
    <span class="keyword">const</span> message = timestamp + '.' + payload;
    <span class="keyword">const</span> expected = crypto
        .createHmac('<span class="string">sha256</span>', secret)
        .update(message)
        .digest('<span class="string">hex</span>');

    <span class="comment">// Сравниваем</span>
    <span class="keyword">return</span> signature === `sha256=${{expected}}`;
}}
                    </div>

                    <div class="section-title">Пример обработчика (Express.js)</div>
                    <div class="code-block">
app.post('<span class="string">/webhook</span>', express.raw({{type: '<span class="string">application/json</span>'}}), (req, res) => {{
    <span class="keyword">const</span> signature = req.headers['<span class="string">x-webhook-signature</span>'];
    <span class="keyword">const</span> timestamp = req.headers['<span class="string">x-webhook-timestamp</span>'];
    <span class="keyword">const</span> event = req.headers['<span class="string">x-webhook-event</span>'];

    <span class="keyword">if</span> (!verifyWebhook(req.body, signature, timestamp, WEBHOOK_SECRET)) {{
        <span class="keyword">return</span> res.status(401).send('<span class="string">Invalid signature</span>');
    }}

    <span class="keyword">const</span> payload = JSON.parse(req.body);

    <span class="keyword">switch</span>(event) {{
        <span class="keyword">case</span> '<span class="string">invoice.confirmed</span>':
            console.log(`Payment confirmed: ${{payload.invoice_id}}`);
            <span class="comment">// Активировать услугу/товар для клиента</span>
            <span class="keyword">break</span>;
        <span class="keyword">case</span> '<span class="string">invoice.expired</span>':
            console.log(`Invoice expired: ${{payload.invoice_id}}`);
            <span class="comment">// Отменить заказ</span>
            <span class="keyword">break</span>;
    }}

    res.status(200).send('<span class="string">OK</span>');
}});
                    </div>

                    <div class="section-title">Ваши Webhooks</div>
                    <div id="webhooksList">
                        <div class="empty-state">Загрузка...</div>
                    </div>

                    <button class="btn btn-primary" style="margin-top: 16px;" onclick="showCreateWebhookModal()">
                        + Добавить Webhook
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- Success Modal -->
    <div class="modal-overlay" id="successModal">
        <div class="modal">
            <h3>Инвойс создан</h3>
            <p>Отправьте ссылку на оплату клиенту:</p>
            <div class="copy-link">
                <input type="text" id="invoiceLink" readonly>
                <button class="btn btn-sm btn-primary" onclick="copyInvoiceLink()">Копировать</button>
            </div>
            <div class="modal-actions">
                <button class="btn btn-primary" onclick="closeModal()">Закрыть</button>
                <button class="btn btn-success" onclick="openInvoice()">Открыть</button>
            </div>
        </div>
    </div>

    <!-- Toast -->
    <div class="toast" id="toast"></div>

    <script>
        const API_KEY = "{api_key}";
        let currentInvoiceUrl = '';

        // API helper
        async function api(method, endpoint, data = null) {{
            const options = {{
                method,
                headers: {{
                    'Authorization': 'Bearer ' + API_KEY,
                    'Content-Type': 'application/json',
                }},
            }};
            if (data) options.body = JSON.stringify(data);

            const response = await fetch('/v1' + endpoint, options);
            if (!response.ok) {{
                const error = await response.json();
                throw new Error(error.detail || 'API Error');
            }}
            return response.json();
        }}

        // Toast
        function showToast(message, isError = false) {{
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast show' + (isError ? ' error' : '');
            setTimeout(() => toast.className = 'toast', 3000);
        }}

        // Load Balances
        async function loadBalances() {{
            try {{
                const data = await api('GET', '/wallets/balances?with_balance_only=false');

                const total = parseFloat(data.total_usdt) + parseFloat(data.total_usdc);
                document.getElementById('totalBalance').textContent = '$' + total.toFixed(2);

                if (data.items.length === 0) {{
                    document.getElementById('balancesGrid').innerHTML =
                        '<div class="empty-state">Нет активных кошельков</div>';
                    return;
                }}

                // Group by chain
                const byChain = {{}};
                data.items.forEach(item => {{
                    if (!byChain[item.chain]) byChain[item.chain] = {{ usdt: 0, usdc: 0, native: 0, symbol: item.native_symbol }};
                    item.tokens.forEach(t => {{
                        if (t.token === 'USDT') byChain[item.chain].usdt += parseFloat(t.balance);
                        if (t.token === 'USDC') byChain[item.chain].usdc += parseFloat(t.balance);
                    }});
                    byChain[item.chain].native += parseFloat(item.native_balance);
                }});

                let html = '';
                for (const [chain, bal] of Object.entries(byChain)) {{
                    const stableTotal = bal.usdt + bal.usdc;
                    if (stableTotal > 0 || bal.native > 0) {{
                        html += `
                            <div class="balance-item">
                                <div class="chain">${{chain}}</div>
                                <div class="amount">${{stableTotal.toFixed(2)}}</div>
                                <div class="token">USDT/USDC</div>
                                <div class="text-muted" style="font-size:12px;margin-top:4px;">
                                    ${{bal.native.toFixed(4)}} ${{bal.symbol}}
                                </div>
                            </div>
                        `;
                    }}
                }}

                document.getElementById('balancesGrid').innerHTML = html ||
                    '<div class="empty-state">Все балансы пусты</div>';
            }} catch (e) {{
                document.getElementById('balancesGrid').innerHTML =
                    '<div class="empty-state">Ошибка загрузки</div>';
            }}
        }}

        // Load Invoices
        async function loadInvoices() {{
            try {{
                const data = await api('GET', '/invoices?limit=50');

                if (data.items.length === 0) {{
                    document.getElementById('invoicesTable').innerHTML =
                        '<tr><td colspan="6" class="empty-state">Нет инвойсов</td></tr>';
                    return;
                }}

                let html = '';
                data.items.forEach(inv => {{
                    const date = new Date(inv.created_at).toLocaleString('ru');
                    const chain = inv.payment?.chain || '-';
                    html += `
                        <tr>
                            <td class="mono">${{inv.public_id}}</td>
                            <td><strong>${{inv.amount}}</strong> ${{inv.asset}}</td>
                            <td><span class="badge badge-${{inv.status.toLowerCase()}}">${{inv.status}}</span></td>
                            <td>${{chain}}</td>
                            <td class="text-muted">${{date}}</td>
                            <td>
                                <button class="btn btn-sm btn-primary" onclick="window.open('${{inv.hosted_url}}', '_blank')">
                                    Открыть
                                </button>
                            </td>
                        </tr>
                    `;
                }});

                document.getElementById('invoicesTable').innerHTML = html;
            }} catch (e) {{
                document.getElementById('invoicesTable').innerHTML =
                    '<tr><td colspan="6" class="empty-state">Ошибка загрузки</td></tr>';
            }}
        }}

        // Create Invoice
        document.getElementById('createInvoiceForm').addEventListener('submit', async (e) => {{
            e.preventDefault();

            const btn = e.target.querySelector('button[type="submit"]');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> Создание...';

            const checkedChains = Array.from(
                document.querySelectorAll('input[name="chains"]:checked')
            ).map(cb => cb.value);

            try {{
                const data = await api('POST', '/invoices', {{
                    amount: document.getElementById('amount').value,
                    asset: document.getElementById('asset').value,
                    allowed_chains: checkedChains,
                    ttl_minutes: parseInt(document.getElementById('ttl').value),
                }});

                currentInvoiceUrl = data.hosted_url;
                document.getElementById('invoiceLink').value = currentInvoiceUrl;
                document.getElementById('successModal').classList.add('show');

                e.target.reset();
                // Re-select all chains
                document.querySelectorAll('.checkbox-item').forEach(el => el.classList.add('selected'));
                document.querySelectorAll('input[name="chains"]').forEach(cb => cb.checked = true);

                loadInvoices();
                showToast('Инвойс создан!');
            }} catch (err) {{
                showToast(err.message, true);
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Создать инвойс';
            }}
        }});

        // Checkbox styling
        document.querySelectorAll('.checkbox-item').forEach(item => {{
            item.addEventListener('click', (e) => {{
                if (e.target.tagName === 'INPUT') return;
                const checkbox = item.querySelector('input');
                checkbox.checked = !checkbox.checked;
                item.classList.toggle('selected', checkbox.checked);
            }});
        }});

        // Modal
        function closeModal() {{
            document.getElementById('successModal').classList.remove('show');
        }}

        function openInvoice() {{
            window.open(currentInvoiceUrl, '_blank');
            closeModal();
        }}

        function copyInvoiceLink() {{
            navigator.clipboard.writeText(currentInvoiceUrl);
            showToast('Ссылка скопирована!');
        }}

        // Tab Navigation
        function showTab(tabId) {{
            // Hide all tabs
            document.querySelectorAll('.tab-content').forEach(tab => {{
                tab.classList.remove('active');
            }});
            document.querySelectorAll('.nav-tab').forEach(btn => {{
                btn.classList.remove('active');
            }});

            // Show selected tab
            document.getElementById('tab-' + tabId).classList.add('active');
            event.target.classList.add('active');

            // Load tab data
            if (tabId === 'balance') {{
                loadDetailedBalances();
                loadSweepJobs();
            }} else if (tabId === 'webhooks') {{
                loadWebhooks();
            }}
        }}

        // Detailed Balances
        async function loadDetailedBalances() {{
            try {{
                const data = await api('GET', '/wallets/balances?with_balance_only=false');

                const total = parseFloat(data.total_usdt) + parseFloat(data.total_usdc);
                document.getElementById('detailedTotalBalance').textContent = '$' + total.toFixed(2);

                if (data.items.length === 0) {{
                    document.getElementById('detailedBalances').innerHTML =
                        '<div class="empty-state">Нет активных кошельков</div>';
                    return;
                }}

                let html = '<div class="balance-grid">';
                data.items.forEach(item => {{
                    let tokensHtml = '';
                    item.tokens.forEach(t => {{
                        if (parseFloat(t.balance) > 0) {{
                            tokensHtml += `<div style="font-size:12px;">${{t.balance}} ${{t.token}}</div>`;
                        }}
                    }});
                    html += `
                        <div class="balance-item">
                            <div class="chain">${{item.chain}}</div>
                            <div style="font-size:11px;color:var(--text-muted);margin:4px 0;">${{item.address.slice(0,10)}}...</div>
                            ${{tokensHtml || '<div style="font-size:12px;color:var(--text-muted);">0 токенов</div>'}}
                            <div style="font-size:11px;margin-top:4px;">${{item.native_balance}} ${{item.native_symbol}}</div>
                        </div>
                    `;
                }});
                html += '</div>';

                document.getElementById('detailedBalances').innerHTML = html;
            }} catch (e) {{
                document.getElementById('detailedBalances').innerHTML =
                    '<div class="empty-state">Ошибка загрузки: ' + e.message + '</div>';
            }}
        }}

        // Sweep Jobs
        async function loadSweepJobs() {{
            try {{
                const data = await api('GET', '/sweep-jobs?limit=20');

                if (!data.items || data.items.length === 0) {{
                    document.getElementById('sweepJobsTable').innerHTML =
                        '<tr><td colspan="5" class="empty-state">Нет sweep операций</td></tr>';
                    return;
                }}

                let html = '';
                data.items.forEach(job => {{
                    const date = new Date(job.created_at).toLocaleString('ru');
                    html += `
                        <tr>
                            <td class="mono">${{job.id.slice(0, 8)}}...</td>
                            <td>${{job.chain}}</td>
                            <td>${{job.amount}} ${{job.token}}</td>
                            <td><span class="badge badge-${{job.state.toLowerCase()}}">${{job.state}}</span></td>
                            <td class="text-muted">${{date}}</td>
                        </tr>
                    `;
                }});

                document.getElementById('sweepJobsTable').innerHTML = html;
            }} catch (e) {{
                document.getElementById('sweepJobsTable').innerHTML =
                    '<tr><td colspan="5" class="empty-state">Ошибка: ' + e.message + '</td></tr>';
            }}
        }}

        // Webhooks
        async function loadWebhooks() {{
            try {{
                const data = await api('GET', '/webhooks');

                if (!data.items || data.items.length === 0) {{
                    document.getElementById('webhooksList').innerHTML =
                        '<div class="empty-state">Webhooks не настроены</div>';
                    return;
                }}

                let html = '';
                data.items.forEach(wh => {{
                    html += `
                        <div class="endpoint-card">
                            <strong>${{wh.url}}</strong>
                            <div style="margin-top:8px;">
                                ${{wh.events.map(e => `<span class="badge badge-info" style="margin-right:4px;">${{e}}</span>`).join('')}}
                            </div>
                            <div style="margin-top:8px;font-size:12px;color:var(--text-muted);">
                                Статус: ${{wh.is_active ? '✅ Активен' : '❌ Неактивен'}}
                            </div>
                        </div>
                    `;
                }});

                document.getElementById('webhooksList').innerHTML = html;
            }} catch (e) {{
                document.getElementById('webhooksList').innerHTML =
                    '<div class="empty-state">Ошибка: ' + e.message + '</div>';
            }}
        }}

        function showCreateWebhookModal() {{
            const url = prompt('Введите URL для webhook:');
            if (!url) return;

            const events = ['invoice.confirmed', 'invoice.expired'];
            api('POST', '/webhooks', {{ url, events }})
                .then(() => {{
                    showToast('Webhook создан!');
                    loadWebhooks();
                }})
                .catch(e => showToast(e.message, true));
        }}

        // Init
        loadBalances();
        loadInvoices();

        // Auto-refresh
        setInterval(() => {{
            loadInvoices();
        }}, 30000);
    </script>
</body>
</html>
    """
