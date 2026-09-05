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
    """Return HMAC-SHA256 for the exact timestamp and body bytes.

    The timestamp limits the acceptance window only. Receivers must durably
    deduplicate verified business events to protect their own side effects.
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

        payload: тело запроса ровно теми байтами, что пришли - до любого
            JSON-парсинга. Переформатированный JSON подпись не пройдёт.
        headers: заголовки запроса.
        secret: непустой секрет webhook, выданный шлюзом при его создании.
        max_age_seconds: положительный допуск по времени в секундах.
    Raises:
        WebhookVerificationError: если что-то не так. Ответьте 400 и не
            обрабатывайте событие.
    """
    if not isinstance(secret, str) or not secret:
        raise WebhookVerificationError("webhook secret is required")
    if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool):
        raise WebhookVerificationError("max_age_seconds must be a positive integer")
    if max_age_seconds <= 0:
        raise WebhookVerificationError("max_age_seconds must be a positive integer")
    signature = _header(headers, HEADER_SIGNATURE)
    raw_timestamp = _header(headers, HEADER_TIMESTAMP)
    header_event = _header(headers, HEADER_EVENT)

    if not signature or not raw_timestamp:
        raise WebhookVerificationError("missing signature headers")

    try:
        timestamp = int(raw_timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("timestamp is not an integer") from exc

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

    event_type = data.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookVerificationError("signed payload is missing event")
    # The header is not covered by the HMAC, so it is informational only and
    # must never override the signed body.
    if header_event is not None and header_event != event_type:
        raise WebhookVerificationError("event header does not match signed payload")

    return WebhookEvent(event_type=event_type, timestamp=timestamp, data=data)
