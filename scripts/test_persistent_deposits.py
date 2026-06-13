#!/usr/bin/env python3
"""
Тестовый скрипт для Persistent Deposits.

1. Создаёт user wallet с адресами во всех сетях
2. Запускает webhook сервер для получения deposit.received
3. Показывает адреса для пополнения

Использование:
    python scripts/test_persistent_deposits.py

После запуска:
    - Скопируйте адрес нужной сети
    - Отправьте USDT/USDC на этот адрес
    - Наблюдайте webhook события в терминале
"""

import asyncio
import json
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx

# Конфигурация
API_BASE = "http://localhost:8123/v1"
WEBHOOK_PORT = 9999
EXTERNAL_USER_ID = f"test_user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# Получаем API key из .env или используем тестовый
API_KEY = None


def load_api_key():
    """Загрузить API key из .env"""
    global API_KEY
    try:
        with open(".env", "r") as f:
            for line in f:
                if "API_KEY" in line and "=" in line:
                    # Ищем строку вида MERCHANT_API_KEY=xxx
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except FileNotFoundError:
        pass

    if not API_KEY:
        # Пробуем получить из тестового мерчанта
        API_KEY = "sk_test_example_key"  # Тестовый ключ

    return API_KEY


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler для webhook событий."""

    def log_message(self, format, *args):
        """Отключаем стандартный лог."""
        pass

    def do_POST(self):
        """Обработка POST запроса (webhook)."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
            self._print_webhook(data)
        except json.JSONDecodeError:
            print(f"⚠️  Invalid JSON: {body[:200]}")

        # Отвечаем 200 OK
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def _print_webhook(self, data):
        """Красиво вывести webhook событие."""
        event = data.get("event", data.get("event_type", "unknown"))

        print("\n" + "=" * 60)
        print(f"🔔 WEBHOOK RECEIVED: {event}")
        print("=" * 60)

        if "data" in data:
            event_data = data["data"]
            print(f"  Chain:      {event_data.get('chain', 'N/A')}")
            print(
                f"  Amount:     {event_data.get('amount', 'N/A')} {event_data.get('asset', '')}"
            )
            print(f"  TX Hash:    {event_data.get('tx_hash', 'N/A')[:20]}...")
            print(f"  From:       {event_data.get('from_address', 'N/A')[:20]}...")
            print(f"  User ID:    {event_data.get('external_user_id', 'N/A')}")
            print(f"  Confirms:   {event_data.get('confirmations', 'N/A')}")
        else:
            print(json.dumps(data, indent=2, ensure_ascii=False))

        print("=" * 60 + "\n")


def start_webhook_server():
    """Запустить webhook сервер в отдельном потоке."""
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    print(f"🌐 Webhook server listening on http://0.0.0.0:{WEBHOOK_PORT}")
    server.serve_forever()


async def setup_webhook(client: httpx.AsyncClient, merchant_api_key: str) -> bool:
    """Настроить webhook для мерчанта."""
    webhook_url = f"http://host.docker.internal:{WEBHOOK_PORT}/webhook"

    # Пробуем создать webhook
    try:
        response = await client.post(
            f"{API_BASE}/webhooks",
            headers={"X-API-Key": merchant_api_key},
            json={
                "url": webhook_url,
                "events": ["deposit.received", "*"],
                "secret": "test_webhook_secret_123",
            },
        )

        if response.status_code == 200:
            print(f"✅ Webhook created: {webhook_url}")
            return True
        elif response.status_code == 409:
            print(f"ℹ️  Webhook already exists")
            return True
        else:
            print(
                f"⚠️  Webhook creation failed: {response.status_code} - {response.text}"
            )
            # Не критично - продолжаем
            return True
    except Exception as e:
        print(f"⚠️  Webhook setup error: {e}")
        return True  # Продолжаем без webhook


async def create_user_wallet(client: httpx.AsyncClient, api_key: str) -> dict | None:
    """Создать кошелёк для пользователя."""
    try:
        response = await client.post(
            f"{API_BASE}/wallets",
            headers={"X-API-Key": api_key},
            json={
                "external_user_id": EXTERNAL_USER_ID,
                "metadata": {
                    "source": "test_script",
                    "created_at": datetime.now().isoformat(),
                },
            },
        )

        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Failed to create wallet: {response.status_code}")
            print(f"   Response: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error creating wallet: {e}")
        return None


async def get_balances(client: httpx.AsyncClient, api_key: str) -> dict:
    """Получить балансы пользователя."""
    try:
        response = await client.get(
            f"{API_BASE}/wallets/{EXTERNAL_USER_ID}/balances",
            headers={"X-API-Key": api_key},
        )
        if response.status_code == 200:
            return response.json()
    except:
        pass
    return {}


async def main():
    """Главная функция."""
    print("\n" + "=" * 60)
    print("🚀 PERSISTENT DEPOSITS TEST")
    print("=" * 60 + "\n")

    # Загружаем API key
    api_key = load_api_key()
    print(f"📝 API Key: {api_key[:8]}...{api_key[-4:]}")
    print(f"👤 User ID: {EXTERNAL_USER_ID}")

    # Запускаем webhook сервер в фоне
    webhook_thread = threading.Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()

    async with httpx.AsyncClient(timeout=30) as client:
        # Проверяем доступность API
        try:
            health = await client.get(f"{API_BASE.replace('/v1', '')}/health")
            if health.status_code != 200:
                print("❌ API not available")
                return
            print("✅ API is healthy")
        except Exception as e:
            print(f"❌ Cannot connect to API: {e}")
            return

        # Настраиваем webhook
        await setup_webhook(client, api_key)

        # Создаём кошелёк
        print("\n📦 Creating user wallet...")
        wallet = await create_user_wallet(client, api_key)

        if not wallet:
            print("❌ Failed to create wallet")
            return

        # Выводим адреса
        print("\n" + "=" * 60)
        print("💰 DEPOSIT ADDRESSES (one address works on all EVM chains)")
        print("=" * 60)

        addresses = wallet.get("addresses", [])

        # Группируем по адресу (они должны быть одинаковые)
        unique_addresses = {}
        for addr in addresses:
            address = addr["address"]
            chain = addr["chain"]
            if address not in unique_addresses:
                unique_addresses[address] = []
            unique_addresses[address].append(chain)

        for address, chains in unique_addresses.items():
            print(f"\n📍 Address: {address}")
            print(f"   Chains:  {', '.join(chains)}")

        print("\n" + "-" * 60)
        print("CHAIN DETAILS:")
        print("-" * 60)

        chain_info = {
            "arbitrum": {
                "explorer": "https://arbiscan.io/address/",
                "name": "Arbitrum One",
            },
            "base": {"explorer": "https://basescan.org/address/", "name": "Base"},
            "bsc": {"explorer": "https://bscscan.com/address/", "name": "BNB Chain"},
            "polygon": {
                "explorer": "https://polygonscan.com/address/",
                "name": "Polygon",
            },
            "avax": {
                "explorer": "https://snowtrace.io/address/",
                "name": "Avalanche C-Chain",
            },
            "optimism": {
                "explorer": "https://optimistic.etherscan.io/address/",
                "name": "Optimism",
            },
        }

        for addr in addresses:
            chain = addr["chain"]
            address = addr["address"]
            info = chain_info.get(chain, {})
            explorer = info.get("explorer", "")
            name = info.get("name", chain)

            print(f"\n  [{chain.upper()}] {name}")
            print(f"  Address: {address}")
            if explorer:
                print(f"  Explorer: {explorer}{address}")

        print("\n" + "=" * 60)
        print("🎯 INSTRUCTIONS:")
        print("=" * 60)
        print("1. Copy the address above")
        print("2. Send USDT or USDC to this address on any supported chain")
        print("3. Watch for webhook events in this terminal")
        print("4. Press Ctrl+C to stop")
        print("=" * 60 + "\n")

        # Периодически показываем балансы
        print("📊 Monitoring balances (updates every 30s)...\n")

        try:
            while True:
                await asyncio.sleep(30)
                balances = await get_balances(client, api_key)
                if balances:
                    print(f"💰 Balances: {balances}")
        except KeyboardInterrupt:
            print("\n\n👋 Stopped by user")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Bye!")
        sys.exit(0)
