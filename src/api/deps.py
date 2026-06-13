"""
Зависимости для API endpoints.
Аутентификация, сессия БД и т.д.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.security import hash_api_key
from src.db.models import ApiKey, Merchant
from src.db.session import get_session

# Type aliases для DI
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# === Admin Authentication ===

# В памяти храним валидные сессии (токен -> время создания)
# В продакшене лучше использовать Redis
_admin_sessions: dict[str, datetime] = {}
_SESSION_TTL_HOURS = 24


def verify_admin_key(provided_key: str, settings: Settings) -> bool:
    """Безопасное сравнение админ-ключа."""
    admin_key = settings.admin_secret_key.get_secret_value()
    if not admin_key:
        return False
    return hmac.compare_digest(provided_key, admin_key)


def create_admin_session() -> str:
    """Создать новую админ-сессию."""
    token = secrets.token_urlsafe(48)
    _admin_sessions[token] = datetime.now(timezone.utc)
    # Очищаем старые сессии
    _cleanup_old_sessions()
    return token


def _cleanup_old_sessions() -> None:
    """Удалить истёкшие сессии."""
    now = datetime.now(timezone.utc)
    expired = [
        token for token, created_at in _admin_sessions.items()
        if (now - created_at).total_seconds() > _SESSION_TTL_HOURS * 3600
    ]
    for token in expired:
        _admin_sessions.pop(token, None)


def validate_admin_session(token: str) -> bool:
    """Проверить валидность сессии."""
    if token not in _admin_sessions:
        return False
    created_at = _admin_sessions[token]
    age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    return age_hours < _SESSION_TTL_HOURS


async def require_admin_auth(
    settings: SettingsDep,
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    authorization: str | None = Header(None),
) -> bool:
    """
    Dependency для защиты админ-эндпоинтов.
    
    Поддерживает два способа авторизации:
    1. X-Admin-Key: <secret_key> - прямой доступ по секретному ключу
    2. Authorization: Bearer <session_token> - доступ по токену сессии
    
    Raises:
        HTTPException 401: Если ключ/токен невалидный
        HTTPException 403: Если админ-ключ не настроен
    """
    admin_key = settings.admin_secret_key.get_secret_value()
    
    # Проверяем настроен ли админ-ключ
    if not admin_key or len(admin_key) < 32:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access not configured. Set ADMIN_SECRET_KEY in .env (min 32 chars)",
        )
    
    # Способ 1: Прямой ключ
    if x_admin_key:
        if verify_admin_key(x_admin_key, settings):
            return True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key",
        )
    
    # Способ 2: Session token
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        if validate_admin_session(token):
            return True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required. Use X-Admin-Key header or Bearer token",
    )


# Type alias для DI
AdminAuthDep = Annotated[bool, Depends(require_admin_auth)]


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

    # last_used_at updates on every polling request can serialize all bots on one
    # api_keys row and stall the API. Keep auth read-only on hot paths.

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
