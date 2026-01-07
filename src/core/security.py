"""
Модуль безопасности: API ключи, HMAC подписи и т.д.
"""

import hashlib
import hmac
import secrets
import time
from typing import Tuple


def create_api_key() -> Tuple[str, str, str]:
    """
    Создать новый API ключ.

    Returns:
        Tuple[str, str, str]: (raw_key, key_prefix, key_hash)
        - raw_key: полный ключ для выдачи пользователю (показывается один раз)
        - key_prefix: первые 8 символов для идентификации
        - key_hash: SHA256 хеш для хранения в БД
    """
    # Генерируем 32 байта случайных данных
    raw_bytes = secrets.token_bytes(32)
    # Кодируем в URL-safe base64
    raw_key = secrets.token_urlsafe(32)
    # Префикс для идентификации ключа в логах и UI
    key_prefix = raw_key[:8]
    # Хеш для безопасного хранения
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    return raw_key, key_prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    """
    Получить хеш API ключа.

    Args:
        raw_key: Сырой API ключ

    Returns:
        SHA256 хеш ключа
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """
    Проверить API ключ.

    Args:
        raw_key: Сырой API ключ от пользователя
        stored_hash: Хеш, хранящийся в БД

    Returns:
        True если ключ валидный
    """
    computed_hash = hash_api_key(raw_key)
    return secrets.compare_digest(computed_hash, stored_hash)


def generate_hmac_signature(
    payload: bytes,
    secret: str,
    timestamp: int | None = None,
) -> Tuple[str, int]:
    """
    Сгенерировать HMAC-SHA256 подпись для webhook.

    Args:
        payload: Тело запроса (raw bytes)
        secret: Секретный ключ webhook
        timestamp: Unix timestamp (если None, используется текущее время)

    Returns:
        Tuple[str, int]: (signature, timestamp)
    """
    if timestamp is None:
        timestamp = int(time.time())

    # Подписываем: timestamp.payload
    message = f"{timestamp}.".encode() + payload
    signature = hmac.new(
        secret.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()

    return signature, timestamp


def verify_hmac_signature(
    payload: bytes,
    secret: str,
    signature: str,
    timestamp: int,
    max_age_seconds: int = 300,  # 5 минут
) -> bool:
    """
    Проверить HMAC-SHA256 подпись webhook.

    Args:
        payload: Тело запроса (raw bytes)
        secret: Секретный ключ webhook
        signature: Подпись из заголовка
        timestamp: Timestamp из заголовка
        max_age_seconds: Максимальный возраст подписи для защиты от replay

    Returns:
        True если подпись валидна и не устарела
    """
    # Проверка времени (защита от replay attack)
    current_time = int(time.time())
    if abs(current_time - timestamp) > max_age_seconds:
        return False

    # Вычисляем ожидаемую подпись
    expected_signature, _ = generate_hmac_signature(payload, secret, timestamp)

    # Безопасное сравнение
    return secrets.compare_digest(expected_signature, signature)


def generate_public_id(prefix: str = "PAY") -> str:
    """
    Сгенерировать публичный ID для инвойса.

    Args:
        prefix: Префикс ID

    Returns:
        Публичный ID вида "PAY_abc123def456"
    """
    random_part = secrets.token_urlsafe(16)[:16]  # 16 символов
    return f"{prefix}_{random_part}"


def generate_webhook_secret() -> str:
    """
    Сгенерировать секрет для webhook.

    Returns:
        Случайный секрет (64 hex символа)
    """
    return secrets.token_hex(32)
