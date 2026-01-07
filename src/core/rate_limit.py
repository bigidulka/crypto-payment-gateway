"""
Rate limiting middleware для API.
"""

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request

from src.core.config import get_settings


def get_api_key_or_ip(request: Request) -> str:
    """
    Идентификатор для rate limiting.
    Использует API key если есть, иначе IP адрес.
    """
    # Пытаемся получить API key из заголовка
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"api:{api_key[:16]}"  # Используем часть ключа для идентификации

    # Fallback на IP адрес
    return get_remote_address(request)


# Создаём limiter с Redis storage для распределённого rate limiting
def create_limiter() -> Limiter:
    """Создать rate limiter."""
    settings = get_settings()

    # Используем Redis как storage если не в debug режиме
    storage_uri = settings.redis_url if not settings.debug else None

    return Limiter(
        key_func=get_api_key_or_ip,
        storage_uri=storage_uri,
        default_limits=["100/minute"],  # Default limit
    )


# Глобальный limiter
limiter = create_limiter()


# Rate limit декораторы для различных endpoints
# Примеры использования:
# @limiter.limit("10/minute")  - 10 запросов в минуту
# @limiter.limit("100/hour")   - 100 запросов в час
# @limiter.limit("5/second")   - 5 запросов в секунду

# Предопределённые лимиты для типичных use-cases
RATE_LIMITS = {
    "create_invoice": "30/minute",  # Создание инвойсов
    "get_invoice": "100/minute",  # Получение инвойсов
    "hosted_page": "60/minute",  # Hosted page загрузки
    "webhook_config": "10/minute",  # Настройка webhooks
    "health_check": "60/minute",  # Health checks
}
