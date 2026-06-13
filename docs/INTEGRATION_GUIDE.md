# 🔌 Руководство по интеграции Crypto Payment Gateway

Полное руководство по интеграции криптоплатёжного шлюза в ваш сайт, бота или приложение.

---

## 📋 Содержание

1. [Быстрый старт](#-быстрый-старт)
2. [Получение API ключа](#-получение-api-ключа)
3. [Создание кошелька пользователя](#-создание-кошелька-пользователя)
4. [Отображение адресов для пополнения](#-отображение-адресов-для-пополнения)
5. [Настройка Webhook](#-настройка-webhook)
6. [Обработка событий](#-обработка-событий)
7. [Проверка балансов](#-проверка-балансов)
8. [Примеры интеграции](#-примеры-интеграции)
9. [Безопасность](#-безопасность)

---

## 🚀 Быстрый старт

### Схема работы

```
┌─────────────┐     1. Регистрация      ┌──────────────────┐
│   Ваш бот   │ ──────────────────────► │  Payment Gateway API    │
│   / сайт    │                         │                  │
└─────────────┘                         └──────────────────┘
       │                                        │
       │  2. Создать кошелёк                    │
       │     для пользователя                   │
       ▼                                        ▼
┌─────────────┐                         ┌──────────────────┐
│ Пользователь│ ◄─ 3. Адреса для ────── │  Wallet Service  │
│             │      пополнения         │                  │
└─────────────┘                         └──────────────────┘
       │                                        │
       │  4. Отправляет                         │
       │     USDT/USDC                          │
       ▼                                        ▼
┌─────────────┐                         ┌──────────────────┐
│  Блокчейн   │ ────────────────────►   │ Persistent Poller│
│  (6 сетей)  │     5. Мониторинг       │                  │
└─────────────┘                         └──────────────────┘
                                                │
                                                │ 6. Webhook
                                                ▼
                                        ┌──────────────────┐
                                        │   Ваш сервер     │
                                        │   (обработка)    │
                                        └──────────────────┘
```

---

## 🔑 Получение API ключа

### Шаг 1: Создание мерчанта

```bash
# Через CLI (если есть доступ к серверу)
python run_cli.py create-merchant \
  --name "My Bot" \
  --webhook-url "https://mybot.com/webhook/payments"
```

Ответ:

```
✅ Merchant created!
   ID: a1b2c3d4-e5f6-...
   API Key: sk_test_example_key...

⚠️  Сохраните API Key — он показывается только один раз!
```

### Шаг 2: Сохранение ключа

```python
# .env вашего бота/сайта
PAYMENT_GATEWAY_API_URL=https://pay.yoursite.com
PAYMENT_GATEWAY_API_KEY=sk_test_example_key...
```

---

## 👛 Создание кошелька пользователя

Каждому пользователю создаётся **персональный кошелёк** с уникальными адресами во всех 6 сетях.

### API Endpoint

```
POST /v1/wallets
Authorization: Bearer {API_KEY}
Content-Type: application/json
```

### Запрос

```json
{
  "external_user_id": "telegram:123456789",
  "metadata": {
    "username": "john_doe",
    "registered_at": "2024-01-15"
  }
}
```

| Параметр           | Тип    | Обязательный | Описание                                   |
| ------------------ | ------ | ------------ | ------------------------------------------ |
| `external_user_id` | string | ✅           | Уникальный ID пользователя в вашей системе |
| `metadata`         | object | ❌           | Произвольные метаданные                    |

### Ответ

```json
{
  "wallet_id": "550e8400-e29b-41d4-a716-446655440000",
  "external_user_id": "telegram:123456789",
  "is_active": true,
  "addresses": [
    {
      "chain": "arbitrum",
      "address": "0x9282F9503416eC2164c34ED6CAD0dCc387C431a6"
    },
    {
      "chain": "base",
      "address": "0x0391C99a79Ea750E9dEF2D2DcF37642A810eD1c2"
    },
    { "chain": "bsc", "address": "0xCc057fC9E26C1817F1ab32823d392F8Eb207E208" },
    {
      "chain": "polygon",
      "address": "0xbf19F5079561728ad6F564E9Bc689b31dB119f97"
    },
    {
      "chain": "optimism",
      "address": "0x1E5aF56B3384e4f2B2269543f2c9a958E95e3B39"
    },
    { "chain": "avax", "address": "0x0b7938E59C7ea5B9853aB356f89b21fFa30De5f5" }
  ],
  "balances": [
    {
      "asset": "USDT",
      "balance": "0",
      "total_deposited": "0",
      "total_withdrawn": "0"
    },
    {
      "asset": "USDC",
      "balance": "0",
      "total_deposited": "0",
      "total_withdrawn": "0"
    }
  ],
  "created_at": "2024-01-15T12:00:00Z"
}
```

### Пример (Python)

```python
import httpx

PAYMENT_GATEWAY_API = "https://pay.yoursite.com"
API_KEY = "sk_test_example_key"

async def create_user_wallet(user_id: str, username: str = None) -> dict:
    """Создать кошелёк для пользователя."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{PAYMENT_GATEWAY_API}/v1/wallets",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "external_user_id": f"telegram:{user_id}",
                "metadata": {"username": username} if username else None
            }
        )
        response.raise_for_status()
        return response.json()
```

### Пример (JavaScript/Node.js)

```javascript
const axios = require("axios");

const PAYMENT_GATEWAY_API = "https://pay.yoursite.com";
const API_KEY = "sk_test_example_key";

async function createUserWallet(userId, username = null) {
  const response = await axios.post(
    `${PAYMENT_GATEWAY_API}/v1/wallets`,
    {
      external_user_id: `telegram:${userId}`,
      metadata: username ? { username } : undefined,
    },
    {
      headers: { Authorization: `Bearer ${API_KEY}` },
    }
  );
  return response.data;
}
```

---

## 💳 Отображение адресов для пополнения

### Получение адреса для конкретной сети

```
GET /v1/wallets/{user_id}/address/{chain}
Authorization: Bearer {API_KEY}
```

```python
async def get_deposit_address(user_id: str, chain: str) -> str:
    """Получить адрес для пополнения в конкретной сети."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PAYMENT_GATEWAY_API}/v1/wallets/telegram:{user_id}/address/{chain}",
            headers={"Authorization": f"Bearer {API_KEY}"}
        )
        data = response.json()
        return data["address"]
```

### Пример для Telegram бота

```python
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Поддерживаемые сети
CHAINS = {
    "arbitrum": {"name": "Arbitrum", "emoji": "🔷", "tokens": "USDT, USDC"},
    "base": {"name": "Base", "emoji": "🔵", "tokens": "USDC"},
    "bsc": {"name": "BSC", "emoji": "🟡", "tokens": "USDT, USDC"},
    "polygon": {"name": "Polygon", "emoji": "🟣", "tokens": "USDT, USDC"},
    "optimism": {"name": "Optimism", "emoji": "🔴", "tokens": "USDT, USDC"},
    "avax": {"name": "Avalanche", "emoji": "🔺", "tokens": "USDT, USDC"},
}

@dp.message_handler(commands=['deposit'])
async def deposit_command(message: types.Message):
    """Показать меню выбора сети для пополнения."""
    keyboard = InlineKeyboardMarkup(row_width=2)

    for chain_id, info in CHAINS.items():
        keyboard.add(InlineKeyboardButton(
            text=f"{info['emoji']} {info['name']}",
            callback_data=f"deposit:{chain_id}"
        ))

    await message.answer(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Выберите сеть для получения адреса:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query_handler(lambda c: c.data.startswith('deposit:'))
async def show_deposit_address(callback: types.CallbackQuery):
    """Показать адрес для пополнения."""
    chain = callback.data.split(':')[1]
    user_id = callback.from_user.id

    # Получаем или создаём кошелёк
    wallet = await create_user_wallet(str(user_id), callback.from_user.username)

    # Находим адрес для выбранной сети
    address = None
    for addr in wallet["addresses"]:
        if addr["chain"] == chain:
            address = addr["address"]
            break

    chain_info = CHAINS[chain]

    await callback.message.edit_text(
        f"{chain_info['emoji']} <b>Пополнение через {chain_info['name']}</b>\n\n"
        f"📮 Адрес для пополнения:\n"
        f"<code>{address}</code>\n\n"
        f"💰 Поддерживаемые токены: {chain_info['tokens']}\n\n"
        f"⚠️ <b>Важно:</b>\n"
        f"• Отправляйте только USDT или USDC\n"
        f"• Минимальная сумма: $1\n"
        f"• Зачисление: 1-5 минут\n",
        parse_mode="HTML"
    )
```

---

## 🔔 Настройка Webhook

Webhook позволяет получать уведомления о депозитах в реальном времени.

### Регистрация Webhook URL

```bash
# При создании мерчанта
python run_cli.py create-merchant \
  --name "My Bot" \
  --webhook-url "https://mybot.com/api/payments/webhook"
```

Или через API:

```
POST /v1/webhooks
Authorization: Bearer {API_KEY}

{
  "url": "https://mybot.com/api/payments/webhook",
  "events": ["deposit.confirmed", "deposit.detected"],
  "secret": "your_webhook_secret_key"
}
```

### Формат Webhook запроса

```http
POST /api/payments/webhook HTTP/1.1
Host: mybot.com
Content-Type: application/json
X-Webhook-Signature: sha256=abc123...
X-Webhook-Timestamp: 1705312800

{
  "event": "deposit.confirmed",
  "timestamp": "2024-01-15T12:00:00Z",
  "data": {
    "deposit_id": "550e8400-e29b-41d4-a716-446655440000",
    "external_user_id": "telegram:123456789",
    "chain": "arbitrum",
    "tx_hash": "0x27c6fb3fa335ad521469...",
    "amount": "10.00",
    "asset": "USDC",
    "confirmations": 15,
    "required_confirmations": 12,
    "new_balance": "10.00"
  }
}
```

---

## 📨 Обработка событий

### Типы событий

| Событие             | Описание                                 |
| ------------------- | ---------------------------------------- |
| `deposit.detected`  | Депозит обнаружен, ожидает подтверждений |
| `deposit.confirmed` | Депозит подтверждён, зачислен на баланс  |
| `deposit.swept`     | Средства переведены в treasury           |

### Пример обработчика (Python/FastAPI)

```python
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
WEBHOOK_SECRET = "your_webhook_secret_key"

def verify_signature(payload: bytes, signature: str, timestamp: str) -> bool:
    """Проверить подпись webhook."""
    message = f"{timestamp}.{payload.decode()}"
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

@app.post("/api/payments/webhook")
async def handle_webhook(request: Request):
    """Обработчик webhook от Payment Gateway."""
    body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    timestamp = request.headers.get("X-Webhook-Timestamp", "")

    # Проверяем подпись
    if not verify_signature(body, signature, timestamp):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    event = data["event"]
    payload = data["data"]

    if event == "deposit.confirmed":
        await handle_deposit_confirmed(payload)

    return {"status": "ok"}

async def handle_deposit_confirmed(payload: dict):
    """Обработка подтверждённого депозита."""
    user_id = payload["external_user_id"].replace("telegram:", "")
    amount = payload["amount"]
    asset = payload["asset"]
    chain = payload["chain"]

    # Обновляем баланс в вашей БД
    await db.update_user_balance(user_id, amount, asset)

    # Уведомляем пользователя
    await bot.send_message(
        chat_id=int(user_id),
        text=f"✅ <b>Пополнение получено!</b>\n\n"
             f"💰 Сумма: {amount} {asset}\n"
             f"🔗 Сеть: {chain}\n"
             f"📊 Новый баланс: {payload['new_balance']} {asset}",
        parse_mode="HTML"
    )
```

### Пример обработчика (Node.js/Express)

```javascript
const express = require("express");
const crypto = require("crypto");

const app = express();
const WEBHOOK_SECRET = "your_webhook_secret_key";

function verifySignature(payload, signature, timestamp) {
  const message = `${timestamp}.${payload}`;
  const expected = `sha256=${crypto
    .createHmac("sha256", WEBHOOK_SECRET)
    .update(message)
    .digest("hex")}`;
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
}

app.post(
  "/api/payments/webhook",
  express.raw({ type: "application/json" }),
  (req, res) => {
    const signature = req.headers["x-webhook-signature"];
    const timestamp = req.headers["x-webhook-timestamp"];

    if (!verifySignature(req.body.toString(), signature, timestamp)) {
      return res.status(401).json({ error: "Invalid signature" });
    }

    const data = JSON.parse(req.body);

    if (data.event === "deposit.confirmed") {
      const { external_user_id, amount, asset, chain, new_balance } = data.data;
      const userId = external_user_id.replace("telegram:", "");

      // Обновляем баланс и уведомляем пользователя
      handleDepositConfirmed(userId, amount, asset, chain, new_balance);
    }

    res.json({ status: "ok" });
  }
);
```

---

## 💰 Проверка балансов

### Получение балансов пользователя

```
GET /v1/wallets/{user_id}/balances
Authorization: Bearer {API_KEY}
```

Ответ:

```json
{
  "USDT": "25.50",
  "USDC": "100.00"
}
```

### Получение истории депозитов

```
GET /v1/wallets/{user_id}/deposits
Authorization: Bearer {API_KEY}
```

Ответ:

```json
{
  "deposits": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "chain": "arbitrum",
      "tx_hash": "0x27c6fb3fa335ad521469...",
      "amount": "10.00",
      "asset": "USDC",
      "status": "confirmed",
      "confirmations": 142,
      "required_confirmations": 12,
      "from_address": "0x3ec68709...",
      "detected_at": "2024-01-15T12:00:00Z",
      "confirmed_at": "2024-01-15T12:01:00Z"
    }
  ],
  "total": 1
}
```

### Пример команды /balance

```python
@dp.message_handler(commands=['balance'])
async def balance_command(message: types.Message):
    """Показать баланс пользователя."""
    user_id = message.from_user.id

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PAYMENT_GATEWAY_API}/v1/wallets/telegram:{user_id}/balances",
            headers={"Authorization": f"Bearer {API_KEY}"}
        )

        if response.status_code == 404:
            await message.answer("У вас ещё нет кошелька. Используйте /deposit")
            return

        balances = response.json()

    text = "💰 <b>Ваш баланс:</b>\n\n"

    total_usd = 0
    for asset, amount in balances.items():
        amount_float = float(amount)
        if amount_float > 0:
            text += f"• {asset}: <b>{amount_float:.2f}</b>\n"
            total_usd += amount_float

    if total_usd == 0:
        text += "<i>Баланс пуст</i>\n"
    else:
        text += f"\n📊 Всего: <b>${total_usd:.2f}</b>"

    await message.answer(text, parse_mode="HTML")
```

---

## 🔒 Безопасность

### 1. Храните API ключ безопасно

```python
# ❌ Плохо
API_KEY = "sk_test_example_key"

# ✅ Хорошо
import os
API_KEY = os.environ["PAYMENT_GATEWAY_API_KEY"]
```

### 2. Всегда проверяйте подпись webhook

```python
# Никогда не обрабатывайте webhook без проверки подписи!
if not verify_signature(body, signature, timestamp):
    raise HTTPException(status_code=401)
```

### 3. Используйте HTTPS

Webhook URL должен использовать HTTPS:

```
✅ https://mybot.com/webhook
❌ http://mybot.com/webhook
```

### 4. Проверяйте timestamp

Отклоняйте webhook если timestamp старше 5 минут:

```python
import time

def verify_webhook(timestamp: str) -> bool:
    webhook_time = int(timestamp)
    current_time = int(time.time())

    # Отклоняем если старше 5 минут
    if abs(current_time - webhook_time) > 300:
        return False
    return True
```

### 5. Идемпотентность

Обрабатывайте каждый `deposit_id` только один раз:

```python
async def handle_deposit_confirmed(payload: dict):
    deposit_id = payload["deposit_id"]

    # Проверяем, не обработан ли уже
    if await db.is_deposit_processed(deposit_id):
        return  # Уже обработано

    # Помечаем как обработанный
    await db.mark_deposit_processed(deposit_id)

    # Обрабатываем...
```

---

## 📚 Полный пример интеграции

### Telegram бот (aiogram 3.x)

```python
import os
import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Конфигурация
BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYMENT_GATEWAY_API = os.environ["PAYMENT_GATEWAY_API_URL"]
API_KEY = os.environ["PAYMENT_GATEWAY_API_KEY"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

CHAINS = {
    "arbitrum": "🔷 Arbitrum",
    "base": "🔵 Base",
    "bsc": "🟡 BSC",
    "polygon": "🟣 Polygon",
    "optimism": "🔴 Optimism",
    "avax": "🔺 Avalanche",
}

@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Команды:\n"
        "/deposit — пополнить баланс\n"
        "/balance — проверить баланс\n"
        "/history — история пополнений"
    )

@router.message(Command("deposit"))
async def deposit_command(message: Message):
    builder = InlineKeyboardBuilder()
    for chain_id, name in CHAINS.items():
        builder.button(text=name, callback_data=f"deposit:{chain_id}")
    builder.adjust(2)

    await message.answer(
        "💳 Выберите сеть для пополнения:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(lambda c: c.data.startswith("deposit:"))
async def show_address(callback: CallbackQuery):
    chain = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)

    async with httpx.AsyncClient() as client:
        # Создаём/получаем кошелёк
        response = await client.post(
            f"{PAYMENT_GATEWAY_API}/v1/wallets",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"external_user_id": f"telegram:{user_id}"}
        )
        wallet = response.json()

    address = next(
        a["address"] for a in wallet["addresses"]
        if a["chain"] == chain
    )

    await callback.message.edit_text(
        f"{CHAINS[chain]}\n\n"
        f"📮 Адрес:\n<code>{address}</code>\n\n"
        f"💡 Отправьте USDT или USDC на этот адрес",
        parse_mode="HTML"
    )

@router.message(Command("balance"))
async def balance_command(message: Message):
    user_id = str(message.from_user.id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PAYMENT_GATEWAY_API}/v1/wallets/telegram:{user_id}/balances",
            headers={"Authorization": f"Bearer {API_KEY}"}
        )

        if response.status_code == 404:
            await message.answer("Кошелёк не найден. Используйте /deposit")
            return

        balances = response.json()

    text = "💰 <b>Баланс:</b>\n\n"
    for asset, amount in balances.items():
        if float(amount) > 0:
            text += f"• {asset}: {float(amount):.2f}\n"

    await message.answer(text, parse_mode="HTML")

dp.include_router(router)

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
```

---

## ❓ FAQ

### Сколько времени занимает зачисление?

От 30 секунд до 5 минут в зависимости от сети и загруженности.

### Какая минимальная сумма депозита?

Технически — любая. Рекомендуем от $1 для покрытия комиссий.

### Какие токены поддерживаются?

USDT и USDC во всех 6 сетях.

### Адрес пользователя меняется?

Нет, адрес постоянный и привязан к пользователю навсегда.

### Что если webhook недоступен?

Система повторяет отправку до 5 раз с экспоненциальной задержкой.

---

## 🆘 Поддержка

- Telegram: @payment_gateway_support
- Email: support@example.com
- Документация: https://example.com/docs
