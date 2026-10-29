"""Telegram checkout connector against a local fake gateway."""

import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbitron_sdk import (  # noqa: E402
    ArbitronClient,
    Screen,
    TelegramCheckout,
    WebhookVerificationError,
)
from arbitron_sdk.webhooks import compute_signature  # noqa: E402

INVOICE = "3f1c9a52-8a8e-4f43-9a51-2a3a2d1b7c01"
ITEM = {
    "invoice_id": INVOICE,
    "telegram_user_id": 42,
    "credit_amount": "10.01",
    "currency": "USD",
    "external_id": f"gateway-{INVOICE}",
    "method": "solana",
    "network": "solana",
    "gross": "10.019",
    "confirmed_at": "2026-10-29T10:00:00+00:00",
    "acked": False,
}
METHODS = {
    "text": "<b>Shop</b>\n\nChoose:",
    "buttons": [
        [{"text": "$10", "callback_data": "ap:abc:a0"}],
        [{"text": "Pay", "url": "https://t.me/pay"}],
    ],
    "expect_input": False,
    "toast": None,
    "paid": False,
}


@pytest.fixture
async def gateway():
    state = SimpleNamespace(
        items=[dict(ITEM)], acked=[], screens=[], next_screen=METHODS, page_cap=100
    )
    app = web.Application()

    async def screen(request):
        body = await request.json()
        assert request.headers["Authorization"] == "Bearer key"
        state.screens.append(body)
        return web.json_response(state.next_screen)

    async def payments(request):
        pending = [i for i in state.items if i["invoice_id"] not in state.acked]
        after = request.query.get("after")
        if after:
            ids = [i["invoice_id"] for i in state.items]
            pending = [i for i in pending if ids.index(i["invoice_id"]) > ids.index(after)]
        # page_cap simulates a gateway page cut short by server-side filtering.
        limit = min(int(request.query["limit"]), state.page_cap)
        return web.json_response({"items": pending[:limit]})

    async def ack(request):
        state.acked.append(request.match_info["invoice_id"])
        return web.json_response({"invoice_id": request.match_info["invoice_id"], "acked": True})

    app.router.add_post("/v1/checkout/telegram", screen)
    app.router.add_get("/v1/checkout/payments", payments)
    app.router.add_post("/v1/checkout/payments/{invoice_id}/ack", ack)
    async with TestServer(app) as server:
        async with ArbitronClient(str(server.make_url("")), "key") as client:
            yield SimpleNamespace(client=client, state=state)


async def test_sync_acks_only_after_credit_returned_truthy(gateway):
    calls = []
    outcome = {"value": RuntimeError("database down")}

    async def credit(user_id, amount, external_id, item):
        calls.append((user_id, amount, external_id))
        value = outcome["value"]
        if isinstance(value, Exception):
            raise value
        return value

    notified = []

    async def notify(user_id, amount, item):
        notified.append((user_id, amount))
        raise RuntimeError("telegram blocked")

    checkout = TelegramCheckout(gateway.client, credit, notify=notify)
    assert await checkout.sync() == 0 and gateway.state.acked == []
    outcome["value"] = False
    assert await checkout.sync() == 0 and gateway.state.acked == []
    outcome["value"] = True
    assert await checkout.sync() == 1 and gateway.state.acked == [INVOICE]
    assert await checkout.sync() == 0
    assert calls == [(42, Decimal("10.01"), f"gateway-{INVOICE}")] * 3
    assert notified == [(42, Decimal("10.01"))]


@pytest.mark.parametrize(
    "change",
    [
        {"external_id": "gateway-other"},
        {"telegram_user_id": True},
        {"telegram_user_id": "42"},
        {"credit_amount": "0"},
        {"credit_amount": "NaN"},
        {"currency": "EUR"},
    ],
)
async def test_malformed_items_are_never_credited(gateway, change):
    gateway.state.items = [{**ITEM, **change}]

    async def credit(*args):
        raise AssertionError("must not credit")

    assert await TelegramCheckout(gateway.client, credit).sync() == 0
    assert gateway.state.acked == []


async def test_screens_track_input_and_paid_screen_syncs(gateway):
    credited = []

    async def credit(user_id, amount, external_id, item):
        credited.append(external_id)
        return True

    checkout = TelegramCheckout(gateway.client, credit)
    assert checkout.is_ours("ap:abc:a0") and not checkout.is_ours("menu:topup")
    screen = await checkout.start(42, "ru")
    assert gateway.state.screens[-1] == {
        "telegram_user_id": 42,
        "lang": "ru",
        "action": {"type": "start"},
    }
    assert screen.reply_markup() == {"inline_keyboard": METHODS["buttons"]}
    gateway.state.next_screen = {**METHODS, "expect_input": True}
    await checkout.callback(42, "ap:abc:c")
    assert checkout.awaiting_input(42) and not checkout.awaiting_input(43)
    gateway.state.next_screen = {**METHODS, "text": None, "toast": "busy"}
    await checkout.input(42, "12.50")
    assert checkout.awaiting_input(42)
    # A rejected amount keeps the input window without extending it.
    deadline = checkout._awaiting[42]
    gateway.state.next_screen = {**METHODS, "expect_input": True}
    await checkout.input(42, "100000")
    assert checkout._awaiting[42] == deadline
    for text in ("12", "7.50", "$7,5", " 7.5 "):
        assert checkout.wants_input(42, text)
    assert not checkout.wants_input(43, "12")
    assert not checkout.wants_input(42, "hello support") and not checkout.awaiting_input(42)
    assert not checkout.wants_input(42, "12")
    gateway.state.next_screen = {**METHODS, "paid": True}
    assert (await checkout.callback(42, "ap:abc:k")).paid
    # The post-payment sync runs in the background, not inside the callback.
    await asyncio.gather(*checkout._tasks)
    assert not checkout.awaiting_input(42) and credited == [f"gateway-{INVOICE}"]
    expiring = TelegramCheckout(gateway.client, credit, input_ttl_seconds=-1)
    gateway.state.next_screen = {**METHODS, "expect_input": True}
    await expiring.start(7)
    assert not expiring.awaiting_input(7)


async def test_sync_pages_past_uncredited_items_and_short_pages(gateway):
    second = "5b2f0a71-6c1d-4a4e-8f0e-1d2c3b4a5f60"
    third = "7c3e1b82-7d2e-4b5f-9a1f-2e3d4c5b6a71"
    gateway.state.items = [
        dict(ITEM),
        {**ITEM, "invoice_id": second, "external_id": f"gateway-{second}"},
        {**ITEM, "invoice_id": third, "external_id": f"gateway-{third}"},
    ]
    gateway.state.page_cap = 1

    async def credit(user_id, amount, external_id, item):
        return external_id != f"gateway-{INVOICE}"

    checkout = TelegramCheckout(gateway.client, credit, page_size=2)
    assert await checkout.sync() == 2
    assert gateway.state.acked == [second, third]


def test_screen_rejects_foreign_buttons():
    for buttons in (
        [[{"text": "x", "callback_data": "menu:1"}]],
        [[{"text": "x", "callback_data": "ap:" + "a" * 70}]],
        [[{"text": "x", "callback_data": "ap:a:b", "url": "https://x"}]],
        [[{"text": "x"}]],
    ):
        with pytest.raises(ValueError):
            Screen.from_api({"text": "t", "buttons": buttons})


async def test_webhook_is_verified_before_sync(gateway):
    async def credit(*args):
        return True

    checkout = TelegramCheckout(gateway.client, credit)
    body = b'{"event":"invoice.confirmed","invoice_id":"x"}'
    now = int(time.time())
    headers = {
        "X-Webhook-Timestamp": str(now),
        "X-Webhook-Signature": compute_signature(body, "secret", now),
    }
    with pytest.raises(WebhookVerificationError):
        await checkout.handle_webhook(body, headers, "other")
    assert gateway.state.acked == []
    assert await checkout.handle_webhook(body, headers, "secret") == 1
    assert gateway.state.acked == [INVOICE]


async def test_aiogram_router_edits_message_and_answers_input(gateway, monkeypatch):
    aiogram = pytest.importorskip("aiogram")
    from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User
    from arbitron_sdk.aiogram import checkout_router, screen_kwargs

    async def credit(*args):
        return True

    checkout = TelegramCheckout(gateway.client, credit)
    kwargs = screen_kwargs(Screen.from_api(METHODS))
    assert kwargs["parse_mode"] == "HTML" and kwargs["text"] == METHODS["text"]
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == "ap:abc:a0"
    assert markup.inline_keyboard[1][0].url == "https://t.me/pay"
    assert screen_kwargs(Screen(text="x"))["reply_markup"] is None

    from aiogram.exceptions import TelegramBadRequest

    router = checkout_router(checkout, lang_of=lambda user: "ru")
    assert isinstance(router, aiogram.Router)
    events = []
    failures = {}

    async def edit_text(self, **kwargs):
        if "edit" in failures:
            raise failures.pop("edit")
        events.append(("edit", kwargs["text"]))

    async def answer_query(self, text=None, **kwargs):
        if "answer" in failures:
            raise failures.pop("answer")
        events.append(("answer", text))

    async def answer_message(self, **kwargs):
        events.append(("message", kwargs.get("text")))

    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(Message, "answer", answer_message)
    monkeypatch.setattr(CallbackQuery, "answer", answer_query)
    user = User(id=42, is_bot=False, first_name="u", language_code="en")
    message = Message.model_construct(
        message_id=1, date=0, chat=Chat(id=42, type="private"), from_user=user, text="12.50"
    )
    query = CallbackQuery.model_construct(
        id="1", from_user=user, chat_instance="c", data="ap:abc:c", message=message
    )
    on_callback = router.callback_query.handlers[0]
    on_message = router.message.handlers[0]
    gateway.state.next_screen = {**METHODS, "expect_input": True, "toast": "hint"}
    await on_callback.call(query)
    # The query is answered before the message is edited.
    assert events == [("answer", "hint"), ("edit", METHODS["text"])]
    assert gateway.state.screens[-1]["lang"] == "ru"
    assert (await on_message.check(message))[0]
    command = Message.model_construct(
        message_id=2, date=0, chat=message.chat, from_user=user, text="/start"
    )
    assert not (await on_message.check(command))[0]
    gateway.state.next_screen = METHODS
    await on_message.call(message)
    assert events[-1] == ("message", METHODS["text"])
    # Ordinary text while the menu waits falls through to the bot's own handlers.
    gateway.state.next_screen = {**METHODS, "expect_input": True}
    await on_callback.call(query)
    chat = Message.model_construct(
        message_id=3, date=0, chat=message.chat, from_user=user, text="help me"
    )
    assert not (await on_message.check(chat))[0] and not checkout.awaiting_input(42)
    events.clear()
    gateway.state.next_screen = {**METHODS, "text": None, "toast": "not yours"}
    await on_callback.call(query)
    assert events == [("answer", "not yours")]
    # An uneditable message gets the screen as a new message.
    events.clear()
    gateway.state.next_screen = METHODS
    failures["edit"] = TelegramBadRequest(method=None, message="message can't be edited")
    await on_callback.call(query)
    assert events == [("answer", None), ("message", METHODS["text"])]
    # A stale query cannot answer, yet the screen is still shown.
    events.clear()
    failures["answer"] = TelegramBadRequest(method=None, message="query is too old")
    await on_callback.call(query)
    assert events == [("edit", METHODS["text"])]
    # An unexpected failure still answers the query.
    events.clear()
    monkeypatch.setattr(checkout, "callback", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        await on_callback.call(query)
    assert events == [("answer", None)]
