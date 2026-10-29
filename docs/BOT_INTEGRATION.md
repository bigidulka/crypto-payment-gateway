# Один шлюз для любого бота или продукта

Код шлюза и SDK подготовлен в ветке `codex/gateway-separation-20261028`.
Это не подтверждение production rollout. Используйте отдельного merchant для
каждого продукта/владельца; токены CryptoBot/xRocket хранятся только в шлюзе.

## Граница ответственности

Бот хранит заказ, цену, идентификатор клиента и права доступа. Шлюз хранит
подключения провайдеров, создаёт счета, подтверждает фактическую оплату и ведёт
расчёты. Боту нужны URL шлюза и merchant API key; для webhook — его отдельный
secret. Ключи не передаются в Telegram-сообщениях, checkout URL или frontend.
Подписка/тариф не становится оплаченным по нажатию кнопки или возврату с checkout.

## Python SDK 0.3.0

SDK поставляется wheel-артефактом из `sdk/python`; он зависит только от aiohttp.
Ни aiogram, ни aiocryptopay, ни блокчейн SDK для продукта не нужны.

```python
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from arbitron_sdk import ArbitronClient, ProductPayments

client = ArbitronClient(gateway_url, merchant_key)
payments = ProductPayments(client, Path(product_orders_database), webhook_secret)

# order_id и цена взяты из доверенного заказа продукта. При потере ответа
# повторить именно этот order_id, не создавать замену.
checkout = await payments.create_order(
    UUID(order_id), customer_id, Decimal("10"), "USDT",
    allowed_chains=["bsc", "solana", "ton"],
    allowed_rails=configured_merchant_rail_ids,
    customer_telegram_id=str(telegram_user_id),
)
# Отправить checkout["hosted_url"] через любой Telegram framework.
```

Список методов должен соответствовать конфигурации конкретного merchant.
Сети TON/Solana не выдаются до их допуска оператором. Для приёма без Telegram
используйте произвольный customer_id; customer_telegram_id нужен только методам,
которые привязывают платёж к Telegram-пользователю. Для JavaScript/Go/других
клиентов используется тот же HTTP-контракт: POST `/v1/invoices` с постоянным
`Idempotency-Key`, GET `/v1/invoices/{id}`, GET `/v1/invoices/{id}/receipt`.

## Подтверждение и выдача доступа

На HTTP webhook передайте исходные bytes и заголовки в
`await payments.receive_webhook(body, headers)`. SDK проверяет подпись, время,
merchant-owned invoice и receipt; повторное событие не создаёт второй заказ.
Без HTTP endpoint или при пропущенном webhook используйте
`await payments.reconcile_order(order_id)` из ограниченного фонового задания.
Оба пути сходятся в одной SQLite-транзакции подтверждения и очереди выдачи.

```python
for item in payments.pending_fulfillments(limit=100):
    # Собственный метод продукта ОБЯЗАН быть идемпотентным по order_id.
    # Это может быть покупка тарифа, пополнение кошелька или выдача API-квоты.
    await product.fulfill_once(item["order_id"], item["customer"], item["receipt"])
    payments.acknowledge_fulfillment(item["order_id"], item["event_id"])
```

При сбое до acknowledge задание остаётся в очереди. SQLite SDK не может
атомарно зафиксировать побочный эффект в другой БД или отправку Telegram;
поэтому exactly-once для внешней выдачи не обещается. Повтор безопасен только
при соблюдении `fulfill_once` на стороне продукта. Для собственной SQL БД
вместо SQLite можно реализовать этот же contract непосредственно в продукте.

Receipt разделяет `gross`, `net` и `provider_fee`. Продукт явно выбирает, как
комиссия влияет на цену и начисление; сумма net не подменяет цену подписки.
Gateway receipt и доступная ликвидность не равны автоматически прибыли владельца.

## Методы

- CryptoBot: токен приложения, account identity, create/read/reconcile и payout
  находятся в gateway. Бот не получает provider credentials.
- xRocket: **Current API**, JWT Bearer; Legacy-токен и старые webhook signatures
  не подходят. Публичная OpenAPI повторно прочитана 28.10.2026, пути адаптера
  присутствуют. Приёмка реального account/invoice/payout ещё требуется.
- Solana: native SOL и classic SPL USDT/USDC; finalized evidence, mint/owner,
  exact units и ATA/rent проверяются отдельно от отправки. Token-2022 и
  автоматическая USD-конвертация не заявляются.
- TON: native TON и USDT jetton; проверяется master, wallet и завершённая
  доставка по trace, не только успешная отправка внешнего сообщения.

Для подключения новых цепочек оператору нужны отдельные seed/treasury/funder,
подтверждённые RPC/indexer, checkpoints и явный opt-in. Эти параметры задаются
на сервере в защищённой конфигурации, а не присылаются в чат.

## PaymentBot и переход с прежней версии

В `GATEWAY_MANAGED=true` меню, старые callback-кнопки и invoice checker работают
через merchant API. CryptoBot token игнорируется, provider SDK не импортируется,
старый admin handler не отправляет чеки. Вывод владельца выполняется отдельным
менеджером через gateway с журналом и резервом.

Отдельный `payment_bot/Dockerfile.gateway` использует компактные зависимости
без aiocryptopay/web3. Старые образы и код совместимости сохраняются для ботов,
которые ещё не переведены. Не включать managed mode автоматически всем ботам.
Сначала сверить и закрыть прежние pending/unknown операции, сохранить balances
и receipts, подготовить merchant и проверить новый путь на одном продукте.
Нельзя перенести старые суммы как новую выручку или повторно оплатить unknown payout.

Новые gateway migrations 0012–0015 и grants требуют отдельной проверки на
PostgreSQL и последующего управляемого rollout. Ни включение флага, ни успешные
unit/VM тесты не заменяют реальную оплату и зачисление в продукте.
