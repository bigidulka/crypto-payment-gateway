"""Необязательный адаптер aiogram 3 для `TelegramCheckout`.

dp.include_router(checkout_router(checkout))   # подключить первым
await send_checkout(checkout, bot, chat_id, user_id, lang)   # кнопка «Пополнить»
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

try:
    from aiogram import F, Router
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        LinkPreviewOptions,
        Message,
    )
except ImportError as exc:  # pragma: no cover - exercised without aiogram
    raise ImportError("arbitron_sdk.aiogram requires aiogram>=3.4") from exc

from .checkout import CALLBACK_PREFIX, Screen, TelegramCheckout
from .client import ArbitronError

log = logging.getLogger(__name__)

UNAVAILABLE = {
    "ru": "Оплата временно недоступна. Попробуйте позже.",
    "en": "Payments are temporarily unavailable. Try again later.",
}


def screen_kwargs(screen: Screen) -> dict[str, Any]:
    """Аргументы для `send_message` / `edit_text` / `answer`."""
    markup = None
    if screen.buttons:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(**b) for b in row] for row in screen.buttons]
        )
    return {
        "text": screen.text,
        "parse_mode": "HTML",
        "reply_markup": markup,
        "link_preview_options": LinkPreviewOptions(is_disabled=True),
    }


async def _language(lang_of: Callable | None, user) -> str | None:
    if lang_of is None:
        return user.language_code
    value = lang_of(user)
    return await value if inspect.isawaitable(value) else value


def _unavailable(lang: str | None) -> str:
    return UNAVAILABLE["ru" if (lang or "").startswith("ru") else "en"]


def checkout_router(
    checkout: TelegramCheckout, *, lang_of: Callable | None = None, name: str = "arbitron-checkout"
) -> Router:
    """Router: кнопки `ap:` редактируют сообщение, ввод своей суммы — ответом."""
    router = Router(name=name)

    @router.callback_query(F.data.startswith(CALLBACK_PREFIX))
    async def on_callback(query: CallbackQuery) -> None:
        answered = False
        try:
            lang = await _language(lang_of, query.from_user)
            try:
                screen = await checkout.callback(query.from_user.id, query.data, lang)
            except ArbitronError:
                answered = True
                await query.answer(_unavailable(lang), show_alert=True)
                return
            # Answer first: Telegram keeps the button spinner until the query is answered.
            answered = True
            try:
                await query.answer(screen.toast)
            except TelegramAPIError as exc:
                # A slow invoice may outlive the query; the screen still has to be shown.
                log.warning("checkout callback answer failed: %s", type(exc).__name__)
            if screen.text is not None:
                await _show(query, screen)
        finally:
            if not answered:
                try:
                    await query.answer()
                except TelegramAPIError:
                    pass

    def awaiting(message: Message) -> bool:
        return (
            message.from_user is not None
            and isinstance(message.text, str)
            and not message.text.startswith("/")
            and checkout.wants_input(message.from_user.id, message.text)
        )

    @router.message(awaiting)
    async def on_input(message: Message) -> None:
        lang = await _language(lang_of, message.from_user)
        try:
            screen = await checkout.input(message.from_user.id, message.text, lang)
        except ArbitronError:
            await message.answer(_unavailable(lang))
            return
        if screen.text is not None:
            await message.answer(**screen_kwargs(screen))

    return router


async def _show(query: CallbackQuery, screen: Screen) -> None:
    """Edit the menu message; if Telegram refuses, send the screen as a new message."""
    kwargs = screen_kwargs(screen)
    message = query.message
    if isinstance(message, Message):
        try:
            await message.edit_text(**kwargs)
            return
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc):
                return
            log.warning("checkout menu edit failed, sending a new message: %s", type(exc).__name__)
        await message.answer(**kwargs)
        return
    chat_id = message.chat.id if message is not None else query.from_user.id
    await query.bot.send_message(chat_id, **kwargs)


async def send_checkout(
    checkout: TelegramCheckout, bot, chat_id: int, user_id: int, lang: str | None = None
) -> Message:
    """Открыть меню оплаты новым сообщением (обработчик вашей кнопки «Пополнить»)."""
    screen = await checkout.start(user_id, lang)
    return await bot.send_message(chat_id, **screen_kwargs(screen))
