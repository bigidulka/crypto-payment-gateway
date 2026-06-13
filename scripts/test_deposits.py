#!/usr/bin/env python3
"""
Тестовый скрипт для Persistent Deposits.
1. Создаёт пользователя и получает адреса для всех сетей
2. Запускает webhook сервер
3. Мониторит балансы и отображает входящие депозиты
"""

import asyncio
import json
import sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import httpx

API_URL = "http://localhost:8123"
API_KEY = "sk_test_example_key"  # Из .env
WEBHOOK_PORT = 9999
USER_ID = "test_user_001"


# Цвета для терминала
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    END = "\033[0m"


def c(text: str, color: str) -> str:
    return f"{color}{text}{Colors.END}"


class WebhookHandler(BaseHTTPRequestHandler):
    """Обработчик webhook событий."""

    def log_message(self, format, *args):
        pass  # Отключаем стандартный лог

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
            event_type = data.get("event", "unknown")

            print(f"\n{c('═' * 60, Colors.YELLOW)}")
            print(f"{c('📬 WEBHOOK EVENT', Colors.BOLD + Colors.YELLOW)}")
            print(f"{c('═' * 60, Colors.YELLOW)}")
            print(f"  {c('Event:', Colors.CYAN)} {event_type}")
            print(f"  {c('Time:', Colors.CYAN)} {datetime.now().strftime('%H:%M:%S')}")

            if event_type == "deposit.detected":
                payload = data.get("data", {})
                print(f"  {c('Chain:', Colors.GREEN)} {payload.get('chain')}")
                print(
                    f"  {c('Amount:', Colors.GREEN)} {payload.get('amount')} {payload.get('asset')}"
                )
                print(f"  {c('TX Hash:', Colors.BLUE)} {payload.get('tx_hash')}")
                print(f"  {c('Block:', Colors.BLUE)} {payload.get('block_number')}")
                print(
                    f"  {c('Confirmations:', Colors.YELLOW)} {payload.get('confirmations')}/{payload.get('required_confirmations')}"
                )

            elif event_type == "deposit.confirmed":
                payload = data.get("data", {})
                print(f"  {c('✅ CONFIRMED!', Colors.BOLD + Colors.GREEN)}")
                print(f"  {c('Chain:', Colors.GREEN)} {payload.get('chain')}")
                print(
                    f"  {c('Amount:', Colors.GREEN)} {payload.get('amount')} {payload.get('asset')}"
                )
                print(
                    f"  {c('New Balance:', Colors.BOLD + Colors.GREEN)} {payload.get('new_balance')}"
                )

            elif event_type == "deposit.swept":
                payload = data.get("data", {})
                print(f"  {c('🧹 SWEPT!', Colors.BOLD + Colors.CYAN)}")
                print(f"  {c('Sweep TX:', Colors.BLUE)} {payload.get('sweep_tx_hash')}")

            else:
                print(f"  {c('Payload:', Colors.CYAN)}")
                print(json.dumps(data, indent=4))

            print(f"{c('═' * 60, Colors.YELLOW)}\n")

        except Exception as e:
            print(f"{c(f'❌ Webhook parse error: {e}', Colors.RED)}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')


def start_webhook_server():
    """Запуск webhook сервера в отдельном потоке."""
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    print(f"{c('🌐 Webhook server started on port', Colors.GREEN)} {WEBHOOK_PORT}")
    server.serve_forever()


async def create_user_wallet() -> dict | None:
    """Создаёт кошелёк для пользователя."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{API_URL}/v1/wallets",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"external_user_id": USER_ID},
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"{c(f'❌ API Error: {response.status_code}', Colors.RED)}")
                print(response.text)
                return None

        except Exception as e:
            print(f"{c(f'❌ Connection error: {e}', Colors.RED)}")
            return None


async def get_balances() -> dict | None:
    """Получает балансы пользователя."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{API_URL}/v1/wallets/{USER_ID}/balances",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )

            if response.status_code == 200:
                return response.json()
            return None

        except Exception:
            return None


async def get_deposits() -> list | None:
    """Получает список депозитов."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{API_URL}/v1/wallets/{USER_ID}/deposits",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("deposits", [])
            return None

        except Exception:
            return None


def print_addresses(wallet: dict):
    """Выводит адреса для каждой сети."""
    addresses = wallet.get("addresses", [])

    print(
        f"\n{c('╔════════════════════════════════════════════════════════════════════════╗', Colors.CYAN)}"
    )
    print(
        f"{c('║', Colors.CYAN)} {c('📮 DEPOSIT ADDRESSES FOR USER:', Colors.BOLD)} {USER_ID:<30} {c('║', Colors.CYAN)}"
    )
    print(
        f"{c('╠════════════════════════════════════════════════════════════════════════╣', Colors.CYAN)}"
    )

    chain_info = {
        "base": ("🔵 Base", "basescan.org"),
        "arbitrum": ("🔷 Arbitrum", "arbiscan.io"),
        "bsc": ("🟡 BSC", "bscscan.com"),
        "polygon": ("🟣 Polygon", "polygonscan.com"),
        "avax": ("🔺 Avalanche", "snowtrace.io"),
        "optimism": ("🔴 Optimism", "optimistic.etherscan.io"),
    }

    for addr_info in addresses:
        chain = addr_info.get("chain")
        address = addr_info.get("address")
        emoji, explorer = chain_info.get(chain, ("⚪", "etherscan.io"))
        print(
            f"{c('║', Colors.CYAN)} {emoji} {c(chain.upper().ljust(10), Colors.GREEN)}: {c(address, Colors.BOLD)} {c('║', Colors.CYAN)}"
        )

    print(
        f"{c('╚════════════════════════════════════════════════════════════════════════╝', Colors.CYAN)}"
    )
    print(
        f"\n{c('💡 Send USDT/USDC to any address above to test deposits', Colors.YELLOW)}"
    )
    print(
        f"{c('   Webhook events will appear below as transactions are detected', Colors.YELLOW)}\n"
    )


async def monitor_loop():
    """Основной цикл мониторинга."""
    last_balance_str = ""
    seen_deposit_ids: set[str] = set()

    while True:
        # Проверяем балансы
        balances = await get_balances()
        if balances:
            balance_str = json.dumps(balances, sort_keys=True)
            if balance_str != last_balance_str:
                last_balance_str = balance_str
                # Проверяем, есть ли ненулевые балансы
                has_balance = any(
                    float(v) > 0 for v in balances.values() if v and v != "0E-18"
                )
                if has_balance:
                    print(f"\n{c('💰 Balances updated:', Colors.GREEN)}")
                    for asset, balance in balances.items():
                        if float(balance) > 0:
                            print(f"   {c(asset, Colors.BOLD)}: {balance}")

        # Проверяем новые депозиты
        deposits = await get_deposits()
        if deposits:
            for dep in deposits:
                dep_id = dep.get("id")
                if dep_id and dep_id not in seen_deposit_ids:
                    seen_deposit_ids.add(dep_id)
                    print(f"\n{c('📥 New deposit detected via API:', Colors.BLUE)}")
                    print(
                        f"   Chain: {dep.get('chain')}, Amount: {dep.get('amount')} {dep.get('asset')}"
                    )
                    print(
                        f"   Status: {dep.get('status')}, Confirmations: {dep.get('confirmations')}/{dep.get('required_confirmations')}"
                    )

        await asyncio.sleep(10)


async def main():
    """Главная функция."""
    print(f"\n{c('='*60, Colors.BLUE)}")
    print(f"{c('  PERSISTENT DEPOSITS TESTER', Colors.BOLD + Colors.BLUE)}")
    print(f"{c('='*60, Colors.BLUE)}\n")

    # Запускаем webhook сервер
    webhook_thread = Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()

    # Создаём кошелёк
    print(f"{c('📝 Creating wallet for user...', Colors.YELLOW)}")
    wallet = await create_user_wallet()

    if not wallet:
        print(f"{c('❌ Failed to create wallet. Is API running?', Colors.RED)}")
        print(f"   Try: docker compose logs api --tail=20")
        return

    print(f"{c('✅ Wallet created!', Colors.GREEN)} ID: {wallet.get('wallet_id')}")

    # Выводим адреса
    print_addresses(wallet)

    print(f"{c('📡 Monitoring for deposits... (Ctrl+C to stop)', Colors.YELLOW)}\n")

    try:
        await monitor_loop()
    except KeyboardInterrupt:
        print(f"\n{c('👋 Stopped monitoring', Colors.YELLOW)}")


if __name__ == "__main__":
    asyncio.run(main())
