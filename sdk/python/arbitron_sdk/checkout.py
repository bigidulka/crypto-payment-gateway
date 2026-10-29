"""Тонкий Telegram-коннектор: шлюз рисует меню оплаты, бот только показывает его и зачисляет.

    checkout = TelegramCheckout(client, credit)

    async def credit(telegram_user_id, amount, external_id, item) -> bool:
        # идемпотентно по external_id; True — сумма зачислена (в том числе раньше)
        ...

Экраны приходят от шлюза готовыми (Telegram HTML + inline-кнопки). Зачисление
идёт только из ленты подтверждённых оплат: `sync()` вызывает `credit` и
подтверждает (ack) позицию только после того, как `credit` вернул истину.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .client import ArbitronClient, ArbitronError
from .webhooks import verify_webhook

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "ap:"
INPUT_TTL_SECONDS = 600
PAGE_SIZE = 50
MAX_PAGES = 20

Credit = Callable[[int, Decimal, str, dict], Awaitable[Any]]
Notify = Callable[[int, Decimal, dict], Awaitable[Any]]


@dataclass(frozen=True)
class Screen:
    """Экран шлюза. `text is None` — сообщение не менять, только показать `toast`."""

    text: str | None
    buttons: tuple[tuple[dict, ...], ...] = ()
    expect_input: bool = False
    toast: str | None = None
    paid: bool = False

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Screen:
        text = data.get("text")
        toast = data.get("toast")
        if text is not None and not isinstance(text, str):
            raise ValueError("screen text must be a string")
        if toast is not None and not isinstance(toast, str):
            raise ValueError("screen toast must be a string")
        rows = []
        for row in data.get("buttons") or []:
            buttons = []
            for button in row:
                label = button.get("text")
                target = {k: button[k] for k in ("callback_data", "url") if k in button}
                if not isinstance(label, str) or len(target) != 1:
                    raise ValueError("screen button needs text and one callback_data or url")
                value = next(iter(target.values()))
                if not isinstance(value, str) or not value:
                    raise ValueError("screen button target must be a string")
                if "callback_data" in target and (
                    not value.startswith(CALLBACK_PREFIX) or len(value.encode()) > 64
                ):
                    raise ValueError("screen callback_data is not a checkout callback")
                buttons.append({"text": label, **target})
            rows.append(tuple(buttons))
        return cls(
            text=text,
            buttons=tuple(rows),
            expect_input=data.get("expect_input") is True,
            toast=toast or None,
            paid=data.get("paid") is True,
        )

    def inline_keyboard(self) -> list[list[dict]]:
        """Кнопки в формате Bot API `InlineKeyboardMarkup.inline_keyboard`."""
        return [[dict(button) for button in row] for row in self.buttons]

    def reply_markup(self) -> dict | None:
        return {"inline_keyboard": self.inline_keyboard()} if self.buttons else None


class TelegramCheckout:
    """
    Коннектор бота к меню оплаты шлюза.

        credit(telegram_user_id, amount, external_id, item) — ваше зачисление.
            Обязано быть идемпотентным по `external_id` (формат `gateway-<invoice id>`)
            и вернуть истину, когда сумма на балансе (в том числе зачислена раньше).
            Исключение или ложь — без ack, позиция придёт снова при следующем sync.
        notify(telegram_user_id, amount, item) — необязательное уведомление
            после зачисления; его ошибки не влияют на зачисление.
    """

    def __init__(
        self,
        client: ArbitronClient,
        credit: Credit,
        *,
        notify: Notify | None = None,
        input_ttl_seconds: int = INPUT_TTL_SECONDS,
        page_size: int = PAGE_SIZE,
    ):
        if not callable(credit):
            raise ValueError("credit callback is required")
        self.client = client
        self._credit = credit
        self._notify = notify
        self._input_ttl = input_ttl_seconds
        self._page_size = page_size
        self._awaiting: dict[int, float] = {}
        self._sync_lock = asyncio.Lock()

    @staticmethod
    def is_ours(callback_data: object) -> bool:
        return isinstance(callback_data, str) and callback_data.startswith(CALLBACK_PREFIX)

    def awaiting_input(self, user_id: int) -> bool:
        deadline = self._awaiting.get(user_id)
        if deadline is None:
            return False
        if deadline < time.monotonic():
            self._awaiting.pop(user_id, None)
            return False
        return True

    async def start(self, user_id: int, lang: str | None = None) -> Screen:
        return await self._screen(user_id, lang, {"type": "start"})

    async def callback(self, user_id: int, data: str, lang: str | None = None) -> Screen:
        return await self._screen(user_id, lang, {"type": "callback", "data": data})

    async def input(self, user_id: int, text: str, lang: str | None = None) -> Screen:
        return await self._screen(user_id, lang, {"type": "input", "text": text[:256]})

    async def _screen(self, user_id: int, lang: str | None, action: dict) -> Screen:
        data = await self.client.checkout_screen(user_id, action, lang=lang)
        try:
            screen = Screen.from_api(data)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ArbitronError("Gateway returned an invalid screen") from exc
        if screen.text is not None:
            if screen.expect_input:
                self._awaiting[user_id] = time.monotonic() + self._input_ttl
            else:
                self._awaiting.pop(user_id, None)
        if screen.paid:
            try:
                await self.sync()
            except Exception:
                log.exception("checkout sync after payment failed; the loop retries")
        return screen

    async def sync(self) -> int:
        """Зачислить подтверждённые оплаты. Возвращает число зачисленных позиций."""
        async with self._sync_lock:
            credited = 0
            for _ in range(MAX_PAGES):
                items = await self.client.checkout_payments(limit=self._page_size)
                progressed = False
                for item in items:
                    if await self._credit_item(item):
                        credited += 1
                        progressed = True
                if len(items) < self._page_size or not progressed:
                    break
            return credited

    async def _credit_item(self, item: Mapping[str, Any]) -> bool:
        try:
            user_id = item["telegram_user_id"]
            amount = Decimal(str(item["credit_amount"]))
            external_id = item["external_id"]
            invoice_id = item["invoice_id"]
            if (
                not isinstance(user_id, int)
                or isinstance(user_id, bool)
                or user_id <= 0
                or not amount.is_finite()
                or amount <= 0
                or item.get("currency") != "USD"
                or external_id != f"gateway-{invoice_id}"
            ):
                raise ValueError("malformed credit item")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            log.error("checkout credit item is malformed; not credited")
            return False
        try:
            if not await self._credit(user_id, amount, external_id, dict(item)):
                return False
        except Exception:
            log.exception("checkout credit %s failed; retried on next sync", external_id)
            return False
        try:
            await self.client.ack_checkout_payment(invoice_id)
        except ArbitronError:
            # Credited but not acknowledged: idempotent credit absorbs the replay.
            log.warning("checkout ack %s failed; retried on next sync", external_id)
            return False
        if self._notify is not None:
            try:
                await self._notify(user_id, amount, dict(item))
            except Exception:
                log.exception("checkout notify %s failed", external_id)
        return True

    async def run_sync_loop(self, interval: float = 30.0) -> None:
        """Фоновая сверка: страхует потерянные вебхуки. Запускайте задачей."""
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("checkout sync failed")
            await asyncio.sleep(interval)

    async def handle_webhook(self, raw: bytes, headers: Mapping[str, str], secret: str) -> int:
        """Проверить подпись вебхука шлюза и забрать ленту зачислений.

        Raises WebhookVerificationError — ответьте 400.
        """
        event = verify_webhook(raw, headers, secret)
        if event.event_type != "invoice.confirmed":
            return 0
        return await self.sync()
