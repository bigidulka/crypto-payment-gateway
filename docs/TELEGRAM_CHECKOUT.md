# Telegram checkout: меню оплаты рисует шлюз

Бот не знает про рельсы, сети, комиссии и счета. Шлюз отдаёт готовые экраны
(Telegram HTML + inline-кнопки), бот их показывает и реализует одну функцию —
идемпотентное `credit(...)`. Зачисление идёт только из ленты подтверждённых оплат.

## Подключение бота (aiogram 3, `arbitron-sdk>=0.4.0`)

```python
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from arbitron_sdk import ArbitronClient, TelegramCheckout
from arbitron_sdk.aiogram import checkout_router, send_checkout

client = ArbitronClient(GATEWAY_URL, GATEWAY_API_KEY)

async def credit(user_id: int, amount, external_id: str, item: dict) -> bool:
    return await db.add_balance(user_id, amount, dedupe_key=external_id)  # идемпотентно

checkout = TelegramCheckout(client, credit)
dp = Dispatcher()
dp.include_router(checkout_router(checkout))  # первым: кнопки ap:* и ввод своей суммы

@dp.callback_query(F.data == "topup")  # ваша кнопка «Пополнить»
async def topup(query: CallbackQuery):
    await send_checkout(checkout, query.bot, query.message.chat.id, query.from_user.id,
                        query.from_user.language_code)
    await query.answer()
```

Плюс фоновая сверка `asyncio.create_task(checkout.run_sync_loop(30))` и, если
есть вебхук шлюза: `await checkout.handle_webhook(raw_body, headers, WEBHOOK_SECRET)`
(неверная подпись → `WebhookVerificationError`, отвечайте 400). Без aiogram:
`checkout.start/callback/input(...)` возвращают `Screen` с `text`, `reply_markup()`
(словарь Bot API), `expect_input`, `toast`; `text is None` — сообщение не менять,
только показать `toast`.

## Контракт зачисления

- `credit(telegram_user_id, amount: Decimal, external_id, item)`; `external_id` —
  `gateway-<invoice uuid>`, тот же формат, что у уже зачисленных счетов LLMASS.
- Идемпотентность по `external_id` обязательна: истина — сумма на балансе
  (в том числе зачислена раньше). Ложь или исключение — без ack, позиция придёт
  снова. Ack уходит только после истины; сбой ack лечится повтором `credit`.
- Сумма в USD (расчёт в USDT 1:1), всегда 2 знака:
  - кастодиальный рельс (CryptoBot/xRocket/Wallet): запрошенная сумма; клиент
    платит её плюс `surcharge_percent` метода (округление вверх до цента);
  - перевод в сети (TON/Solana): запрошенная сумма плюс переплата сверх счёта,
    вниз до цента. Недоплата и просрочка не зачисляются никогда.
- Старые счета с метаданными `telegram_user_id` + `product` (например
  `llmass-topup`) тоже попадают в ленту: рельс — сумма счёта, сеть — пришедшее
  вниз до цента (как зачислял LLMASS).

## API шлюза (Bearer-ключ мерчанта)

| Метод | Путь | Назначение |
|---|---|---|
| GET/PUT | `/v1/checkout/profile` | профиль меню мерчанта |
| POST | `/v1/checkout/telegram` | следующий экран |
| GET | `/v1/checkout/payments?limit=50` | подтверждённые оплаты без ack |
| GET | `/v1/checkout/payments/{invoice_id}` | одна позиция (404 — не подтверждена) |
| POST | `/v1/checkout/payments/{invoice_id}/ack` | идемпотентный ack |

Профиль: `product_title`, `currency_label` (`USD`), `preset_amounts`,
`min_amount`, `max_amount`, `custom_amount`, `methods`
(`[{method: <rail uuid>|"ton"|"solana", label, surcharge_percent}]`),
`default_language` (`ru`/`en`), `invoice_ttl_minutes`. Суммы — строки с ≤2
знаками; рельсы проверяются на принадлежность мерчанту и USDT, сети — на
включённый on-chain шлюз.

Экран: запрос `{"telegram_user_id", "lang", "action"}`, где `action` —
`{"type":"start"}`, `{"type":"callback","data":"ap:…"}` или
`{"type":"input","text":"…"}`; ответ `{"text", "buttons", "expect_input",
"toast", "paid"}`. `callback_data` — `ap:<sid>:<op>`, ≤64 байт; сессия в Redis
привязана к пользователю (чужое нажатие — только toast), повторное и
параллельное нажатие не выпускают второй счёт.

Ленту будит существующий вебхук `invoice.confirmed`; новых событий нет.

## Развёртывание

1. Миграция `0018_telegram_checkout` (owner): `checkout_profiles`,
   `checkout_payment_acks` (append-only, триггер запрещает UPDATE/DELETE/TRUNCATE).
2. `scripts/provision_application_runtime_role.sql`: runtime получает
   `SELECT, INSERT, UPDATE` на `checkout_profiles` и только `SELECT, INSERT` на
   `checkout_payment_acks`.
3. Нужен Redis (`REDIS_URL`) — состояние меню и блокировки.
4. `PUT /v1/checkout/profile` для мерчанта, затем подключение бота как выше.
