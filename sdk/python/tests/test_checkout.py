"""Telegram checkout connector against a local fake gateway."""

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
    state = SimpleNamespace(items=[dict(ITEM)], acked=[], screens=[], next_screen=METHODS)
    app = web.Application()

    async def screen(request):
        body = await request.json()
        assert request.headers["Authorization"] == "Bearer key"
        state.screens.append(body)
        return web.json_response(state.next_screen)

    async def payments(request):
        return web.json_response(
            {"items": [i for i in state.items if i["invoice_id"] not in state.acked]}
        )

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
    gateway.state.next_screen = {**METHODS, "paid": True}
    assert (await checkout.callback(42, "ap:abc:k")).paid
    assert not checkout.awaiting_input(42) and credited == [f"gateway-{INVOICE}"]
    expiring = TelegramCheckout(gateway.client, credit, input_ttl_seconds=-1)
    gateway.state.next_screen = {**METHODS, "expect_input": True}
    await expiring.start(7)
    assert not expiring.awaiting_input(7)


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

    router = checkout_router(checkout, lang_of=lambda user: "ru")
    assert isinstance(router, aiogram.Router)
    edits, answers, replies = [], [], []

    async def edit_text(self, **kwargs):
        edits.append(kwargs)

    async def answer_query(self, text=None, **kwargs):
        answers.append(text)

    async def answer_message(self, **kwargs):
        replies.append(kwargs)

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
    assert edits[-1]["text"] == METHODS["text"] and answers == ["hint"]
    assert gateway.state.screens[-1]["lang"] == "ru"
    assert (await on_message.check(message))[0]
    command = Message.model_construct(
        message_id=2, date=0, chat=message.chat, from_user=user, text="/start"
    )
    assert not (await on_message.check(command))[0]
    gateway.state.next_screen = METHODS
    await on_message.call(message)
    assert replies[-1]["text"] == METHODS["text"]
    gateway.state.next_screen = {**METHODS, "text": None, "toast": "not yours"}
    await on_callback.call(query)
    assert len(edits) == 1 and answers[-1] == "not yours"
