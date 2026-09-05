"""
Arbitron Payment Gateway - Python SDK.

Тонкий асинхронный клиент над HTTP API шлюза. Ничего не знает про Telegram,
ботов или чью-либо модель пользователей: `external_user_id` - произвольная
строка на стороне интегратора.

    from arbitron_sdk import ArbitronClient, verify_webhook

    async with ArbitronClient(api_url, api_key) as client:
        invoice = await client.create_invoice(
            amount=Decimal("10"), asset="USDT",
            allowed_chains=["bsc", "arbitrum"],
            external_user_id="shop:42",
        )
        # покажите пользователю invoice.hosted_url, либо
        selection = await client.select_payment(invoice.public_id, chain="bsc", token="USDT")

    # на входящем вебхуке:
    event = verify_webhook(body_bytes, headers, secret)
"""

from .client import ArbitronClient, ArbitronError
from .models import (
    ChainInfo,
    Invoice,
    InvoiceStatus,
    PaymentSelection,
    PaymentStatus,
    WebhookEvent,
)
from .webhooks import WebhookVerificationError, verify_webhook

__version__ = "0.1.0"

__all__ = [
    "ArbitronClient",
    "ArbitronError",
    "ChainInfo",
    "Invoice",
    "InvoiceStatus",
    "PaymentSelection",
    "PaymentStatus",
    "WebhookEvent",
    "WebhookVerificationError",
    "verify_webhook",
    "__version__",
]
