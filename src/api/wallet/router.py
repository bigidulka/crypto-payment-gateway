"""
API Router для User Wallets (Persistent Deposits).

Позволяет мерчантам создавать постоянные кошельки для своих пользователей.
"""

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import MerchantDep, SessionDep
from src.blockchain.chains import get_all_chains
from src.services.user_wallet_service import UserWalletService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wallets", tags=["User Wallets"])


# === Schemas ===


class WalletAddressResponse(BaseModel):
    """Адрес кошелька в сети."""

    chain: str
    address: str

    class Config:
        from_attributes = True


class UserBalanceResponse(BaseModel):
    """Баланс пользователя по активу."""

    asset: str
    balance: str
    total_deposited: str
    total_withdrawn: str

    class Config:
        from_attributes = True


class UserWalletResponse(BaseModel):
    """Полная информация о кошельке пользователя."""

    external_user_id: str
    is_active: bool
    addresses: list[WalletAddressResponse]
    balances: list[UserBalanceResponse]
    created_at: str


class CreateWalletRequest(BaseModel):
    """Запрос на создание кошелька."""

    external_user_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Уникальный ID пользователя в вашей системе (telegram_id, user_id и т.д.)",
        examples=["telegram:123456789", "user_12345"],
    )
    metadata: dict | None = Field(
        default=None,
        description="Дополнительные метаданные пользователя",
        examples=[{"username": "john_doe", "registered_at": "2024-01-01"}],
    )


class DepositResponse(BaseModel):
    """Информация о депозите."""

    id: str
    chain: str
    tx_hash: str
    amount: str
    asset: str
    status: str
    confirmations: int
    required_confirmations: int
    from_address: str
    detected_at: str
    confirmed_at: str | None

    class Config:
        from_attributes = True


class DepositHistoryResponse(BaseModel):
    """История депозитов."""

    deposits: list[DepositResponse]
    total: int


class AllDepositsDepositResponse(BaseModel):
    """Депозит с информацией о пользователе."""

    id: str
    external_user_id: str
    chain: str
    tx_hash: str
    amount: str
    asset: str
    status: str
    confirmations: int
    required_confirmations: int
    from_address: str
    detected_at: str
    confirmed_at: str | None


class AllDepositsResponse(BaseModel):
    """Все депозиты мерчанта."""

    deposits: list[AllDepositsDepositResponse]
    total: int
    page: int
    per_page: int
    has_more: bool


# === Endpoints ===


@router.get(
    "/deposits",
    response_model=AllDepositsResponse,
    summary="Все депозиты мерчанта",
)
async def get_all_merchant_deposits(
    merchant: MerchantDep,
    session: SessionDep,
    page: Annotated[int, Query(ge=1, description="Номер страницы")] = 1,
    per_page: Annotated[
        int, Query(ge=1, le=100, description="Записей на странице")
    ] = 50,
    status: Annotated[
        str | None, Query(description="Фильтр: pending, confirmed")
    ] = None,
    chain: Annotated[
        str | None, Query(description="Фильтр по сети: arbitrum, base, bsc...")
    ] = None,
    since: Annotated[
        str | None, Query(description="ISO дата, депозиты после: 2026-01-07T00:00:00")
    ] = None,
) -> AllDepositsResponse:
    """
    Получить все депозиты мерчанта по всем пользователям.

    Используйте для polling вместо вебхуков:
    - Периодически опрашивайте этот эндпоинт
    - Используйте `since` для получения только новых депозитов
    - Фильтруйте по `status=confirmed` для подтверждённых

    **Пример polling:**
    ```
    GET /v1/wallets/deposits?since=2026-01-07T12:00:00&status=confirmed
    ```
    """
    from datetime import datetime

    service = UserWalletService(session)

    # Парсим since если передан
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date format: {since}. Use ISO format: 2026-01-07T00:00:00",
            )

    offset = (page - 1) * per_page

    deposits, total = await service.get_all_deposits(
        merchant_id=merchant.id,
        limit=per_page,
        offset=offset,
        status=status,
        chain=chain,
        since=since_dt,
    )

    return AllDepositsResponse(
        deposits=[
            AllDepositsDepositResponse(
                id=str(dep.id),
                external_user_id=dep.user_wallet.external_user_id,
                chain=dep.chain,
                tx_hash=dep.tx_hash,
                amount=str(dep.amount),
                asset=dep.asset,
                status=dep.status.value,
                confirmations=dep.confirmations,
                required_confirmations=dep.required_confirmations,
                from_address=dep.from_address,
                detected_at=dep.detected_at.isoformat(),
                confirmed_at=dep.confirmed_at.isoformat() if dep.confirmed_at else None,
            )
            for dep in deposits
        ],
        total=total,
        page=page,
        per_page=per_page,
        has_more=(offset + len(deposits)) < total,
    )


@router.post(
    "",
    response_model=UserWalletResponse,
    summary="Создать кошелёк для пользователя",
)
async def create_user_wallet(
    request: CreateWalletRequest,
    merchant: MerchantDep,
    session: SessionDep,
) -> UserWalletResponse:
    """
    Создать или получить постоянный кошелёк для пользователя.

    При первом вызове создаёт кошелёк с адресами во всех поддерживаемых сетях.
    При повторных вызовах возвращает существующий кошелёк.

    **Адреса во всех сетях:**
    - Arbitrum
    - Base
    - BSC
    - Polygon
    - Avalanche
    - Optimism

    **Поддерживаемые токены:**
    - USDT
    - USDC
    """
    service = UserWalletService(session)

    wallet = await service.get_or_create_wallet(
        merchant_id=merchant.id,
        external_user_id=request.external_user_id,
        user_metadata=request.metadata,
    )

    return UserWalletResponse(
        external_user_id=wallet.external_user_id,
        is_active=wallet.is_active,
        addresses=[
            WalletAddressResponse(
                chain=addr.chain,
                address=addr.address,
            )
            for addr in wallet.addresses
        ],
        balances=[
            UserBalanceResponse(
                asset=bal.asset,
                balance=str(bal.balance),
                total_deposited=str(bal.total_deposited),
                total_withdrawn=str(bal.total_withdrawn),
            )
            for bal in wallet.balances
        ],
        created_at=wallet.created_at.isoformat(),
    )


@router.get(
    "/{external_user_id}",
    response_model=UserWalletResponse,
    summary="Получить кошелёк пользователя",
)
async def get_user_wallet(
    external_user_id: str,
    merchant: MerchantDep,
    session: SessionDep,
) -> UserWalletResponse:
    """
    Получить информацию о кошельке пользователя.

    Включает:
    - Адреса во всех сетях
    - Балансы по всем активам
    """
    service = UserWalletService(session)

    wallet = await service.get_wallet(
        merchant_id=merchant.id,
        external_user_id=external_user_id,
    )

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found for user: {external_user_id}",
        )

    return UserWalletResponse(
        external_user_id=wallet.external_user_id,
        is_active=wallet.is_active,
        addresses=[
            WalletAddressResponse(
                chain=addr.chain,
                address=addr.address,
            )
            for addr in wallet.addresses
        ],
        balances=[
            UserBalanceResponse(
                asset=bal.asset,
                balance=str(bal.balance),
                total_deposited=str(bal.total_deposited),
                total_withdrawn=str(bal.total_withdrawn),
            )
            for bal in wallet.balances
        ],
        created_at=wallet.created_at.isoformat(),
    )


@router.get(
    "/{external_user_id}/address/{chain}",
    summary="Получить адрес для конкретной сети",
)
async def get_wallet_address(
    external_user_id: str,
    chain: str,
    merchant: MerchantDep,
    session: SessionDep,
) -> WalletAddressResponse:
    """
    Получить deposit адрес пользователя для конкретной сети.

    Удобно для отображения QR-кода или копирования адреса.
    """
    if chain not in get_all_chains():
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported chain: {chain}. Supported: {get_all_chains()}",
        )

    service = UserWalletService(session)

    wallet = await service.get_wallet(
        merchant_id=merchant.id,
        external_user_id=external_user_id,
    )

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found for user: {external_user_id}",
        )

    address = next(
        (addr for addr in wallet.addresses if addr.chain == chain),
        None,
    )

    if not address:
        raise HTTPException(
            status_code=404,
            detail=f"Address not found for chain: {chain}",
        )

    return WalletAddressResponse(
        chain=address.chain,
        address=address.address,
    )


@router.get(
    "/{external_user_id}/balances",
    summary="Получить балансы пользователя",
)
async def get_user_balances(
    external_user_id: str,
    merchant: MerchantDep,
    session: SessionDep,
) -> dict[str, str]:
    """
    Получить балансы пользователя по всем активам.

    Returns:
        Словарь {asset: balance}
    """
    service = UserWalletService(session)

    balances = await service.get_user_balances(
        merchant_id=merchant.id,
        external_user_id=external_user_id,
    )

    if not balances:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found for user: {external_user_id}",
        )

    return {asset: str(balance) for asset, balance in balances.items()}


@router.get(
    "/{external_user_id}/deposits",
    response_model=DepositHistoryResponse,
    summary="История депозитов пользователя",
)
async def get_deposit_history(
    external_user_id: str,
    merchant: MerchantDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DepositHistoryResponse:
    """
    Получить историю депозитов пользователя.

    Возвращает последние депозиты с информацией о статусе и подтверждениях.
    """
    service = UserWalletService(session)

    deposits = await service.get_deposit_history(
        merchant_id=merchant.id,
        external_user_id=external_user_id,
        limit=limit,
    )

    return DepositHistoryResponse(
        deposits=[
            DepositResponse(
                id=str(dep.id),
                chain=dep.chain,
                tx_hash=dep.tx_hash,
                amount=str(dep.amount),
                asset=dep.asset,
                status=dep.status.value,
                confirmations=dep.confirmations,
                required_confirmations=dep.required_confirmations,
                from_address=dep.from_address,
                detected_at=dep.detected_at.isoformat(),
                confirmed_at=dep.confirmed_at.isoformat() if dep.confirmed_at else None,
            )
            for dep in deposits
        ],
        total=len(deposits),
    )


@router.get(
    "",
    summary="Список кошельков мерчанта",
)
async def list_merchant_wallets(
    merchant: MerchantDep,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """
    Получить список всех кошельков мерчанта с пагинацией.
    """
    from sqlalchemy import func, select
    from src.db.models import UserWallet

    # Подсчёт общего количества
    count_stmt = select(func.count(UserWallet.id)).where(
        UserWallet.merchant_id == merchant.id
    )
    total = await session.scalar(count_stmt) or 0

    # Получение кошельков
    offset = (page - 1) * per_page
    stmt = (
        select(UserWallet)
        .where(UserWallet.merchant_id == merchant.id)
        .order_by(UserWallet.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    result = await session.execute(stmt)
    wallets = result.scalars().all()

    return {
        "wallets": [
            {
                "external_user_id": w.external_user_id,
                "is_active": w.is_active,
                "created_at": w.created_at.isoformat(),
            }
            for w in wallets
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }
