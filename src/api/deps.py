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

# Токен сессии - подписанный, без состояния: "<expires_ts>.<nonce>.<hmac>".
# Подпись на ADMIN_SECRET_KEY, поэтому токен переживает рестарт процесса и
# одинаково валиден на любой реплике API. Раньше сессии жили в dict в памяти
# и каждый деплой выбрасывал всех админов из панели.
_SESSION_TTL_HOURS = 24
_TOKEN_DERIVATION_TAG = b"arbitron-admin-session-v1"


def verify_admin_key(provided_key: str, settings: Settings) -> bool:
    """Безопасное сравнение админ-ключа."""
    admin_key = settings.admin_secret_key.get_secret_value()
    if not admin_key:
        return False
    return hmac.compare_digest(provided_key, admin_key)


def _session_signing_key(settings: Settings) -> bytes:
    """
    Ключ подписи выводится из админ-ключа, а не равен ему: утечка подписи
    токена не должна отдавать сам ADMIN_SECRET_KEY.
    """
    admin_key = settings.admin_secret_key.get_secret_value().encode()
    return hmac.new(admin_key, _TOKEN_DERIVATION_TAG, hashlib.sha256).digest()


def _sign_session(payload: str, settings: Settings) -> str:
    return hmac.new(
        _session_signing_key(settings), payload.encode(), hashlib.sha256
    ).hexdigest()


def create_admin_session(settings: Settings) -> str:
    """Выпустить админ-токен на _SESSION_TTL_HOURS."""
    expires = int(datetime.now(timezone.utc).timestamp()) + _SESSION_TTL_HOURS * 3600
    nonce = secrets.token_urlsafe(16)
    payload = f"{expires}.{nonce}"
    return f"{payload}.{_sign_session(payload, settings)}"


def validate_admin_session(token: str, settings: Settings) -> bool:
    """Проверить подпись и срок токена."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expires_raw, nonce, signature = parts
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if int(datetime.now(timezone.utc).timestamp()) >= expires:
        return False
    expected = _sign_session(f"{expires_raw}.{nonce}", settings)
    return hmac.compare_digest(expected, signature)


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
        if validate_admin_session(token, settings):
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
