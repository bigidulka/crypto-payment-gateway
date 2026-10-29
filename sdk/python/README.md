# SDK 0.3.0: подключение продукта без CryptoBot SDK

Готовый durable checkout, webhook/polling и очередь выдачи доступа: `ProductPayments`.
Полный пример и правила повторов: [BOT_INTEGRATION](../../docs/BOT_INTEGRATION.md).
Пакет 0.3.0 подготовлен локально; публикация в package registry не выполнялась.

# arbitron-sdk

Python-клиент платёжного шлюза Arbitron. Принимает крипто-оплату в
нескольких сетях, отдаёт статус и подписанные вебхуки. Ничего не знает про
Telegram — подходит любому проекту.

## Установка

Текущая локальная версия — 0.2.0. Она собрана в wheel для ботов и ещё не
опубликована. Сначала установите согласованный release wheel; следующий Git URL
получает опубликованную версию репозитория и не включает локальную ветку.

```bash
pip install "arbitron-sdk @ git+https://github.com/bigidulka/arbitron-payment.git#subdirectory=sdk/python"
```

## Три шага

### 1. Ключ

Получите `api_key` и `webhook_secret` у оператора шлюза (создаются через
admin API вместе с мерчантом и вебхуком).

### 2. Счёт

```python
from decimal import Decimal
from arbitron_sdk import ArbitronClient

async with ArbitronClient("https://pay.example.com", api_key) as client:
    invoice = await client.create_invoice(
        amount=Decimal("10"),
        asset="USDT",
        allowed_chains=["bsc", "arbitrum", "polygon"],
        external_user_id="shop:42",       # ваш id плательщика, любая строка
        idempotency_key=f"order-{order_id}",  # повтор вернёт тот же счёт
    )
    # Самый простой путь - отправить пользователя на hosted-страницу:
    print(invoice.hosted_url)

    # Либо свой UI: выбираете сеть, показываете адрес и QR
    sel = await client.select_payment(invoice.public_id, chain="bsc", token="USDT")
    print(sel.deposit_address, sel.qr_data)
```

Список сетей и токенов — `await client.get_chains()`. У каждой сети есть
`estimated_credit_seconds`: честное время до зачисления, которое можно
показать пользователю.

### 3. Вебхук

```python
from arbitron_sdk import verify_webhook, WebhookVerificationError

@app.post("/webhooks/arbitron")
async def on_webhook(request):
    body = await request.body()  # ровно те байты, что пришли
    try:
        event = verify_webhook(body, request.headers, webhook_secret)
    except WebhookVerificationError:
        return Response(status_code=400)

    invoice = event.data["invoice"]
    if event.event_type == "invoice.confirmed":
        # invoice["amount"] is the requested amount, not a verified received
        # settlement amount. Do not credit from this webhook payload alone.
        queue_reconciliation(invoice["id"])
    return Response(status_code=200)
```

Подпись — HMAC-SHA256 над `"{timestamp}." + body`. `event` берётся только из
подписанного JSON body; `X-Webhook-Event` — информационный header и при
расхождении с body запрос отклоняется. Заголовки: `X-Webhook-Signature`,
`X-Webhook-Timestamp`, `X-Webhook-Event`. Окно допуска по времени — 5 минут:
оно ограничивает возраст подписи, но **не заменяет дедупликацию**. Храните
обработанный durable ключ (например, `invoice.id` вместе с типом terminal
event) в своей транзакции. Поле `invoice.amount` — запрошенная сумма: при
переплате/недоплате оно не является суммой к зачислению. До появления в
подписанном payload verified received amount сверяйте merchant-scoped
authoritative payment/ledger record и делайте итоговое credit идемпотентным.

## Сверка статуса

Вебхук может не дойти. Для надёжности опрашивайте:

```python
status = await client.get_payment_status(invoice.public_id)
if status.is_paid:
    ...
elif status.mismatch_reason == "underpaid":
    # пришло меньше: status.received_amount, не хватает status.missing_amount
elif status.mismatch_reason == "wrong_token":
    # прислали другой токен: status.mismatch_token
```

Шлюз сам засчитывает недоплату в пределах допуска (биржи удерживают комиссию
с вывода) и закрывает счёт доплатой вторым переводом на тот же адрес.

## Ошибки

Всё сетевое и HTTP ≥ 400 — `ArbitronError` с полями `status` и `body`.
`outcome_unknown=True` после mutating timeout/5xx/неверного ответа означает,
что операция могла выполниться. Сохраните прежний ключ и сверяйте операцию;
не создавайте новый ключ для повторной отправки денег.

## CryptoBot, xRocket, Wallet Pay

Provider methods доступны только для merchant, включённого оператором в
allowlist. Merchant подключает своё приложение; токен не передаётся покупателю.

```python
rail_id = await client.connect_provider(
    provider="cryptobot", token=provider_token, assets=["USDT"],
)
invoice = await client.create_invoice(
    amount="10", asset="USDT", allowed_chains=[], allowed_rails=[rail_id],
    external_user_id="shop:42", idempotency_key=f"order-{order_id}",
)
selected = await client.select_provider(invoice.public_id, rail_id)
# selected.pay_url может быть None: счёт требует проверки, не повторной отправки.

balance = (await client.provider_balances(rail_id))[0]
if balance.available is not None and balance.available > 0:
    operation = await client.payout(
        rail_id=rail_id, amount=str(balance.available), asset="USDT",
        destination=trusted_telegram_user_id,
        idempotency_key=f"withdrawal-{withdrawal_id}",
    )
```

`available` содержит подтверждённую сумму на вывод с учётом ликвидности и
резервов. `None` сохраняет UNKNOWN; подставлять вместо неё ledger balance
нельзя. Wallet Pay имеет ручной вывод. CryptoBot возвращает привязанный чек,
xRocket — transfer. Потерянная отправка сохраняет резерв.

`await client.invoice_receipt(invoice_id)` даёт merchant-owned `gross`, `net`,
`provider_fee` и стабильный `event_id` только после подтверждённого journal.
Сверяйте этот receipt с подписанным event и своим сохранённым заказом.
Пример с durable заказом и дедупликацией после рестарта:
[product_payment.py](../../examples/product_payment.py).
API и rollout: [provider-payments.md](../../docs/provider-payments.md).

## 0.4.0: меню оплаты в Telegram рисует шлюз

`TelegramCheckout(client, credit)` показывает экраны шлюза и зачисляет только
подтверждённые оплаты из ленты: `credit(telegram_user_id, amount, external_id, item)`
идемпотентно по `external_id`, ack — только после истины. Адаптер aiogram 3
(`pip install arbitron-sdk[aiogram]`): `arbitron_sdk.aiogram.checkout_router`
и `send_checkout`. Подробно и пример бота: `docs/TELEGRAM_CHECKOUT.md` шлюза.
