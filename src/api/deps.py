"""
Зависимости для API endpoints.
Аутентификация, сессия БД и т.д.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.security import hash_api_key
from src.db.models import ApiKey, Merchant
from src.db.session import get_session

# Type aliases для DI
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_merchant(
    session: SessionDep,
    authorization: str = Header(..., description="Bearer <api_key>"),
) -> Merchant:
    """
    Dependency для получения текущего мерчанта по API ключу.

    Извлекает API ключ из заголовка Authorization: Bearer <key>
    и возвращает связанного мерчанта.

    Raises:
        HTTPException 401: Если ключ невалидный или мерчант неактивен
    """
    # Извлекаем токен из заголовка
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Use: Bearer <api_key>",
        )

    api_key = authorization[7:]  # Убираем "Bearer "
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is required",
        )

    # Хешируем ключ для поиска
    key_hash = hash_api_key(api_key)

    # Ищем ключ в БД
    stmt = (
        select(ApiKey)
        .where(ApiKey.key_hash == key_hash)
        .where(ApiKey.is_active == True)  # noqa: E712
    )
    result = await session.execute(stmt)
    api_key_record = result.scalar_one_or_none()

    if api_key_record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Загружаем мерчанта
    stmt = (
        select(Merchant)
        .where(Merchant.id == api_key_record.merchant_id)
        .where(Merchant.is_active == True)  # noqa: E712
    )
    result = await session.execute(stmt)
    merchant = result.scalar_one_or_none()

    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Merchant account is inactive",
        )

    # Обновляем last_used_at (без commit - будет в конце запроса)
    # Используем try/except чтобы не блокировать запрос при ошибке SQLite
    try:
        api_key_record.last_used_at = datetime.now(timezone.utc)
        # НЕ делаем commit здесь - сессия закроется в конце запроса
    except Exception:
        pass  # last_used_at не критично

    return merchant


# Type alias для DI
MerchantDep = Annotated[Merchant, Depends(get_current_merchant)]


def get_idempotency_key(
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> str | None:
    """
    Dependency для получения Idempotency-Key из заголовка.

    Используется для защиты от дублирования запросов.
    """
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
