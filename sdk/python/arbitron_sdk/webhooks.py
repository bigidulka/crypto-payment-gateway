"""Проверка подписи входящих вебхуков шлюза."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

from .models import WebhookEvent

HEADER_SIGNATURE = "X-Webhook-Signature"
HEADER_TIMESTAMP = "X-Webhook-Timestamp"
HEADER_EVENT = "X-Webhook-Event"

DEFAULT_MAX_AGE_SECONDS = 300


class WebhookVerificationError(ValueError):
    """Подпись не сошлась, протухла или заголовки неполные."""


def compute_signature(payload: bytes, secret: str, timestamp: int) -> str:
    """
    HMAC-SHA256 над `"{timestamp}." + payload`.

    Timestamp внутри подписанного сообщения - защита от replay: перехваченный
    вебхук нельзя переиграть позже с другим временем.
    """
    message = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Заголовки бывают в любом регистре в зависимости от фреймворка."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify_webhook(
    payload: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: int | None = None,
) -> WebhookEvent:
    """
    Проверить вебхук и вернуть распарсенное событие.

    Args:
        payload: тело запроса ровно теми байтами, что пришли - до любого
            JSON-парсинга. Переформатированный JSON подпись не пройдёт.
        headers: заголовки запроса.
        secret: секрет вебхука, выданный шлюзом при его создании.
        max_age_seconds: допуск по времени; ноль отключает проверку.

    Raises:
        WebhookVerificationError: если что-то не так. Ответьте 400 и не
            обрабатывайте событие.
    """
    signature = _header(headers, HEADER_SIGNATURE)
    raw_timestamp = _header(headers, HEADER_TIMESTAMP)
    event_type = _header(headers, HEADER_EVENT) or ""

    if not signature or not raw_timestamp:
        raise WebhookVerificationError("missing signature headers")

    try:
        timestamp = int(raw_timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("timestamp is not an integer") from exc

    if max_age_seconds:
        current = int(time.time()) if now is None else now
        if abs(current - timestamp) > max_age_seconds:
            raise WebhookVerificationError("signature too old")

    expected = compute_signature(payload, secret, timestamp)
    if not secrets.compare_digest(expected, signature):
        raise WebhookVerificationError("signature mismatch")

    try:
        data: Any = json.loads(payload)
    except ValueError as exc:
        raise WebhookVerificationError("payload is not JSON") from exc
    if not isinstance(data, dict):
        raise WebhookVerificationError("payload must be a JSON object")

    return WebhookEvent(
        event_type=event_type or str(data.get("event", "")),
        timestamp=timestamp,
        data=data,
    )
