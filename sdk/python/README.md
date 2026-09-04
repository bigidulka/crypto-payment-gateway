# arbitron-sdk

Python-клиент платёжного шлюза Arbitron. Принимает крипто-оплату в
нескольких сетях, отдаёт статус и подписанные вебхуки. Ничего не знает про
Telegram — подходит любому проекту.

## Установка

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

    if event.event_type == "invoice.confirmed":
        credit_user(event.data["metadata"]["external_user_id"], event.data["amount"])
    return Response(status_code=200)
```

Подпись — HMAC-SHA256 над `"{timestamp}." + body`, заголовки
`X-Webhook-Signature`, `X-Webhook-Timestamp`, `X-Webhook-Event`. Окно
допуска по времени 5 минут (защита от replay).

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
