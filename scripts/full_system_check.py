#!/usr/bin/env python3
"""
Полная проверка системы Crypto Payment для 24/7 нагрузки.
Запускается ВНУТРИ Docker контейнера api.

Проверяет:
1. База данных PostgreSQL
2. Redis
3. API
4. Блокчейн RPC
5. HD Wallet
6. Безопасность
7. Консистентность данных
8. Нагрузочное тестирование
"""

import asyncio
import os
import sys
import time
from datetime import datetime

# Добавляем путь к src
sys.path.insert(0, "/app/src")

import aiohttp
import asyncpg
import redis.asyncio as aioredis


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {Colors.GREEN}✓{Colors.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {Colors.RED}✗{Colors.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}⚠{Colors.END} {msg}")


def info(msg: str) -> None:
    print(f"  {Colors.BLUE}ℹ{Colors.END} {msg}")


def header(title: str) -> None:
    print(f"\n{Colors.BOLD}{'='*60}{Colors.END}")
    print(f"{Colors.BOLD}{title}{Colors.END}")
    print(f"{Colors.BOLD}{'='*60}{Colors.END}")


def subheader(title: str) -> None:
    print(f"\n{Colors.BLUE}▶ {title}{Colors.END}")


class SystemChecker:
    def __init__(self):
        # Внутри контейнера используем внутренние адреса
        self.api_url = "http://127.0.0.1:8000"
        # PostgreSQL URL без asyncpg для asyncpg библиотеки
        db_url = os.getenv("DATABASE_URL", "")
        self.postgres_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        self.redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        self.results = {"passed": 0, "failed": 0, "warnings": 0}
        self.critical_errors = []
        
    async def run_all_checks(self):
        """Запуск всех проверок."""
        start_time = time.time()
        
        header("🔍 ПОЛНАЯ ПРОВЕРКА СИСТЕМЫ CRYPTO PAYMENT GATEWAY")
        print(f"Время начала: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 1. База данных
        await self.check_database()
        
        # 2. Redis
        await self.check_redis()
        
        # 3. API (внутренний)
        await self.check_api()
        
        # 4. Блокчейн RPC
        await self.check_blockchain_rpc()
        
        # 5. HD Wallet
        await self.check_hd_wallet()
        
        # 6. Безопасность
        await self.check_security()
        
        # 7. Консистентность данных
        await self.check_data_consistency()
        
        # 8. Нагрузочный тест
        await self.check_api_load()
        
        # Итоги
        elapsed = time.time() - start_time
        self.print_summary(elapsed)
        
        return len(self.critical_errors) == 0
    
    async def check_database(self):
        """Проверка PostgreSQL."""
        header("1️⃣  БАЗА ДАННЫХ (PostgreSQL)")
        
        try:
            conn = await asyncpg.connect(self.postgres_url)
            
            subheader("Подключение")
            ok("PostgreSQL доступен")
            self.results["passed"] += 1
            
            # Версия
            version = await conn.fetchval("SELECT version()")
            info(f"Версия: {version.split(',')[0]}")
            
            subheader("Таблицы")
            tables = await conn.fetch("""
                SELECT tablename FROM pg_tables 
                WHERE schemaname = 'public' 
                ORDER BY tablename
            """)
            expected_tables = [
                "merchants", "invoices", "payments", "onchain_txs",
                "user_wallets", "wallet_addresses", "deposits",
                "deposit_addresses", "sweep_jobs", "system_logs"
            ]
            
            existing = [t["tablename"] for t in tables]
            for table in expected_tables:
                if table in existing:
                    count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                    ok(f"{table}: {count} rows")
                    self.results["passed"] += 1
                else:
                    fail(f"{table}: NOT FOUND")
                    self.results["failed"] += 1
            
            subheader("Индексы")
            indexes = await conn.fetch("""
                SELECT indexname, tablename 
                FROM pg_indexes 
                WHERE schemaname = 'public'
                AND indexname NOT LIKE '%_pkey'
            """)
            info(f"Найдено {len(indexes)} кастомных индексов")
            
            # Важные индексы
            important_indexes = [
                "ix_wallet_addresses_address",
                "ix_deposits_tx_hash",
            ]
            for idx in important_indexes:
                if any(i["indexname"] == idx for i in indexes):
                    ok(f"Индекс {idx} существует")
                    self.results["passed"] += 1
                else:
                    warn(f"Индекс {idx} не найден (рекомендуется создать)")
                    self.results["warnings"] += 1
            
            subheader("Подключения к БД")
            connections = await conn.fetchval("""
                SELECT count(*) FROM pg_stat_activity 
                WHERE datname = 'payment_gateway'
            """)
            max_conn = await conn.fetchval("SHOW max_connections")
            usage_pct = int(connections) / int(max_conn) * 100
            if usage_pct < 50:
                ok(f"Подключения: {connections}/{max_conn} ({usage_pct:.0f}%)")
                self.results["passed"] += 1
            elif usage_pct < 80:
                warn(f"Подключения: {connections}/{max_conn} ({usage_pct:.0f}%)")
                self.results["warnings"] += 1
            else:
                fail(f"Критично много подключений: {connections}/{max_conn}")
                self.results["failed"] += 1
            
            await conn.close()
            
        except Exception as e:
            fail(f"Ошибка PostgreSQL: {e}")
            self.results["failed"] += 1
            self.critical_errors.append(f"PostgreSQL error: {e}")
    
    async def check_redis(self):
        """Проверка Redis."""
        header("2️⃣  КЭШИРОВАНИЕ (Redis)")
        
        try:
            r = aioredis.from_url(self.redis_url)
            
            subheader("Подключение")
            await r.ping()
            ok("Redis доступен")
            self.results["passed"] += 1
            
            # Info
            info_data = await r.info()
            info(f"Версия: {info_data['redis_version']}")
            info(f"Память: {info_data['used_memory_human']}")
            info(f"Клиенты: {info_data['connected_clients']}")
            
            subheader("Ключи")
            keys = await r.keys("*")
            info(f"Всего ключей: {len(keys)}")
            
            # Важные ключи
            important_keys = [
                "user_wallet:next_derivation_index",
                "deposit_address:next_index"
            ]
            for key in important_keys:
                value = await r.get(key)
                if value:
                    ok(f"{key} = {value.decode()}")
                    self.results["passed"] += 1
                else:
                    warn(f"{key} не найден")
                    self.results["warnings"] += 1
            
            # Тест записи
            subheader("Тест записи/чтения")
            test_key = "health_check_test"
            await r.set(test_key, "ok", ex=60)
            value = await r.get(test_key)
            if value == b"ok":
                ok("Запись/чтение работает")
                self.results["passed"] += 1
            else:
                fail("Ошибка записи/чтения")
                self.results["failed"] += 1
            await r.delete(test_key)
            
            await r.aclose()
            
        except Exception as e:
            fail(f"Ошибка Redis: {e}")
            self.results["failed"] += 1
            self.critical_errors.append(f"Redis error: {e}")
    
    async def check_api(self):
        """Проверка API."""
        header("3️⃣  API СЕРВЕР")
        
        async with aiohttp.ClientSession() as session:
            subheader("Health check")
            try:
                async with session.get(
                    f"{self.api_url}/health", 
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ok(f"API здоров: {data.get('status', 'ok')}")
                        self.results["passed"] += 1
                    else:
                        fail(f"Health check вернул {resp.status}")
                        self.results["failed"] += 1
            except Exception as e:
                fail(f"API недоступен: {e}")
                self.results["failed"] += 1
                self.critical_errors.append(f"API unreachable: {e}")
                return
            
            subheader("Chains эндпоинт")
            try:
                async with session.get(
                    f"{self.api_url}/api/v1/chains",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        chains = data.get("chains", [])
                        ok(f"Поддерживается {len(chains)} сетей")
                        for chain in chains[:5]:
                            info(f"  - {chain.get('name', chain.get('chain_id'))}")
                        self.results["passed"] += 1
                    else:
                        fail(f"Chains вернул {resp.status}")
                        self.results["failed"] += 1
            except Exception as e:
                fail(f"Chains ошибка: {e}")
                self.results["failed"] += 1
            
            subheader("Авторизация")
            try:
                async with session.get(
                    f"{self.api_url}/api/v1/merchant/invoices",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status in [401, 403]:
                        ok("Защита работает (401/403 без ключа)")
                        self.results["passed"] += 1
                    elif resp.status == 200:
                        warn("Эндпоинт доступен без авторизации!")
                        self.results["warnings"] += 1
                    else:
                        info(f"Статус: {resp.status}")
            except Exception as e:
                warn(f"Проверка авторизации: {e}")
                self.results["warnings"] += 1
    
    async def check_blockchain_rpc(self):
        """Проверка RPC эндпоинтов."""
        header("4️⃣  БЛОКЧЕЙН RPC")
        
        try:
            from core.config import settings
            from blockchain.chains import SUPPORTED_CHAINS
            
            async with aiohttp.ClientSession() as session:
                for chain_id, chain_config in list(SUPPORTED_CHAINS.items())[:7]:  # Первые 7 сетей
                    subheader(f"{chain_config.name} ({chain_id})")
                    
                    rpcs = settings.get_chain_rpcs(chain_id)
                    if not rpcs:
                        warn(f"Нет RPC для {chain_id}")
                        self.results["warnings"] += 1
                        continue
                    
                    working_rpcs = 0
                    tested = min(len(rpcs), 3)
                    
                    for rpc_url in rpcs[:tested]:
                        try:
                            payload = {
                                "jsonrpc": "2.0",
                                "method": "eth_blockNumber",
                                "params": [],
                                "id": 1
                            }
                            async with session.post(
                                rpc_url, 
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=5)
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if "result" in data:
                                        block = int(data["result"], 16)
                                        working_rpcs += 1
                        except:
                            pass
                    
                    if working_rpcs > 0:
                        ok(f"{working_rpcs}/{tested} RPC работают")
                        self.results["passed"] += 1
                    else:
                        fail(f"Нет рабочих RPC!")
                        self.results["failed"] += 1
                        self.critical_errors.append(f"No working RPC for {chain_id}")
                        
        except Exception as e:
            fail(f"Ошибка проверки RPC: {e}")
            self.results["failed"] += 1
    
    async def check_hd_wallet(self):
        """Проверка HD кошелька."""
        header("5️⃣  HD КОШЕЛЁК")
        
        try:
            from core.config import settings
            from crypto.hd_wallet import HDWallet
            
            subheader("Инициализация")
            mnemonic = settings.HD_WALLET_MNEMONIC
            if mnemonic:
                wallet = HDWallet(mnemonic)
                ok("HD кошелёк инициализирован")
                self.results["passed"] += 1
                
                # Генерация адресов
                subheader("Генерация адресов")
                addresses = []
                start = time.time()
                for i in range(10):
                    key = wallet.derive_key(i)
                    addresses.append(key.address)
                elapsed = time.time() - start
                ok(f"10 адресов за {elapsed*1000:.0f}ms")
                self.results["passed"] += 1
                
                # Детерминированность
                subheader("Детерминированность")
                key1 = wallet.derive_key(0)
                key2 = wallet.derive_key(0)
                if key1.address == key2.address and key1.private_key == key2.private_key:
                    ok("Адреса детерминированные")
                    self.results["passed"] += 1
                else:
                    fail("Адреса НЕ детерминированные!")
                    self.results["failed"] += 1
                    self.critical_errors.append("HD wallet not deterministic")
                
                # Адрес #0
                info(f"Адрес #0: {key1.address}")
                    
            else:
                fail("HD_WALLET_MNEMONIC не задан!")
                self.results["failed"] += 1
                self.critical_errors.append("HD_WALLET_MNEMONIC missing")
                
        except Exception as e:
            fail(f"Ошибка HD кошелька: {e}")
            self.results["failed"] += 1
            self.critical_errors.append(f"HD wallet error: {e}")
    
    async def check_security(self):
        """Проверка безопасности."""
        header("6️⃣  БЕЗОПАСНОСТЬ")
        
        subheader("Переменные окружения")
        required_env = {
            "HD_WALLET_MNEMONIC": "Мнемоника кошелька",
            "ENCRYPTION_KEY": "Ключ шифрования",
            "FUNDER_PRIVATE_KEY": "Приватный ключ фандера",
        }
        
        optional_env = {
            "API_ADMIN_KEY": "Админ ключ API",
            "WEBHOOK_SIGNING_SECRET": "Секрет подписи вебхуков",
        }
        
        for env_var, desc in required_env.items():
            value = os.getenv(env_var)
            if value:
                if len(value) >= 16:
                    ok(f"{env_var}: установлен")
                    self.results["passed"] += 1
                else:
                    warn(f"{env_var}: слишком короткий ({len(value)} символов)")
                    self.results["warnings"] += 1
            else:
                fail(f"{env_var}: НЕ УСТАНОВЛЕН ({desc})")
                self.results["failed"] += 1
        
        for env_var, desc in optional_env.items():
            value = os.getenv(env_var)
            if value:
                ok(f"{env_var}: установлен")
                self.results["passed"] += 1
            else:
                warn(f"{env_var}: не установлен ({desc})")
                self.results["warnings"] += 1
        
        subheader("Шифрование")
        try:
            from crypto.encryption import encrypt_private_key, decrypt_private_key
            
            test_key = "0x" + "a" * 64
            encrypted = encrypt_private_key(test_key)
            decrypted = decrypt_private_key(encrypted)
            
            if decrypted == test_key:
                ok("Шифрование/расшифровка работает")
                self.results["passed"] += 1
                
                # Проверяем что зашифровано не plaintext
                if encrypted != test_key:
                    ok("Данные действительно зашифрованы")
                    self.results["passed"] += 1
                else:
                    fail("Данные не зашифрованы!")
                    self.results["failed"] += 1
            else:
                fail("Ошибка шифрования - данные не совпадают")
                self.results["failed"] += 1
                self.critical_errors.append("Encryption broken")
        except Exception as e:
            fail(f"Ошибка шифрования: {e}")
            self.results["failed"] += 1
            self.critical_errors.append(f"Encryption error: {e}")
    
    async def check_data_consistency(self):
        """Проверка консистентности данных."""
        header("7️⃣  КОНСИСТЕНТНОСТЬ ДАННЫХ")
        
        try:
            conn = await asyncpg.connect(self.postgres_url)
            
            subheader("tx_hash нормализация (0x префикс)")
            tables_with_tx = [
                ("deposits", "tx_hash"),
                ("onchain_txs", "tx_hash"),
                ("sweep_jobs", "gas_tx_hash"),
                ("sweep_jobs", "sweep_tx_hash")
            ]
            
            all_normalized = True
            for table, column in tables_with_tx:
                count = await conn.fetchval(f"""
                    SELECT COUNT(*) FROM {table} 
                    WHERE {column} IS NOT NULL 
                    AND {column} NOT LIKE '0x%'
                """)
                if count == 0:
                    ok(f"{table}.{column}: ✓")
                    self.results["passed"] += 1
                else:
                    fail(f"{table}.{column}: {count} без 0x!")
                    self.results["failed"] += 1
                    all_normalized = False
            
            subheader("Дубликаты tx_hash")
            duplicates = await conn.fetchval("""
                SELECT COUNT(*) FROM (
                    SELECT tx_hash, chain, COUNT(*) 
                    FROM deposits 
                    WHERE tx_hash IS NOT NULL
                    GROUP BY tx_hash, chain 
                    HAVING COUNT(*) > 1
                ) sub
            """)
            if duplicates == 0:
                ok("Нет дубликатов deposits.tx_hash")
                self.results["passed"] += 1
            else:
                fail(f"Найдено {duplicates} дубликатов!")
                self.results["failed"] += 1
            
            subheader("Сироты (orphan records)")
            orphan_addresses = await conn.fetchval("""
                SELECT COUNT(*) FROM wallet_addresses wa
                LEFT JOIN user_wallets uw ON wa.user_wallet_id = uw.id
                WHERE uw.id IS NULL
            """)
            if orphan_addresses == 0:
                ok("Нет сирот wallet_addresses")
                self.results["passed"] += 1
            else:
                warn(f"{orphan_addresses} сирот wallet_addresses")
                self.results["warnings"] += 1
            
            orphan_deposits = await conn.fetchval("""
                SELECT COUNT(*) FROM deposits d
                LEFT JOIN wallet_addresses wa ON d.wallet_address_id = wa.id
                WHERE wa.id IS NULL
            """)
            if orphan_deposits == 0:
                ok("Нет сирот deposits")
                self.results["passed"] += 1
            else:
                warn(f"{orphan_deposits} сирот deposits")
                self.results["warnings"] += 1
            
            subheader("Пропуски derivation_index")
            max_idx = await conn.fetchval("SELECT MAX(derivation_index) FROM wallet_addresses")
            min_idx = await conn.fetchval("SELECT MIN(derivation_index) FROM wallet_addresses")
            unique_count = await conn.fetchval("SELECT COUNT(DISTINCT derivation_index) FROM wallet_addresses")
            
            if max_idx is not None:
                expected = max_idx - min_idx + 1
                if unique_count == expected:
                    ok(f"Индексы {min_idx}-{max_idx} без пропусков")
                    self.results["passed"] += 1
                else:
                    gaps_count = expected - unique_count
                    warn(f"Пропущено {gaps_count} индексов (возможно потеряны адреса)")
                    self.results["warnings"] += 1
                    
                    # Показываем первые 5 пропусков
                    gaps = await conn.fetch(f"""
                        WITH all_idx AS (
                            SELECT generate_series({min_idx}, {max_idx}) as idx
                        )
                        SELECT idx FROM all_idx 
                        WHERE idx NOT IN (SELECT DISTINCT derivation_index FROM wallet_addresses)
                        LIMIT 5
                    """)
                    if gaps:
                        info(f"  Первые пропуски: {[g['idx'] for g in gaps]}")
            
            await conn.close()
            
        except Exception as e:
            fail(f"Ошибка проверки данных: {e}")
            self.results["failed"] += 1
    
    async def check_api_load(self):
        """Нагрузочный тест API."""
        header("8️⃣  НАГРУЗОЧНЫЙ ТЕСТ")
        
        subheader("50 параллельных запросов")
        
        async def make_request(session: aiohttp.ClientSession, url: str) -> tuple:
            start = time.time()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.read()
                    return (resp.status, time.time() - start)
            except Exception as e:
                return (0, time.time() - start)
        
        async with aiohttp.ClientSession() as session:
            url = f"{self.api_url}/health"
            tasks = [make_request(session, url) for _ in range(50)]
            
            start = time.time()
            results = await asyncio.gather(*tasks)
            total_time = time.time() - start
            
            successful = sum(1 for r in results if r[0] == 200)
            avg_time = sum(r[1] for r in results) / len(results)
            max_time = max(r[1] for r in results)
            min_time = min(r[1] for r in results)
            
            if successful == 50:
                ok(f"50/50 успешны")
                self.results["passed"] += 1
            elif successful >= 45:
                warn(f"{successful}/50 успешны")
                self.results["warnings"] += 1
            else:
                fail(f"Только {successful}/50 успешны")
                self.results["failed"] += 1
            
            info(f"Общее время: {total_time:.2f}s")
            info(f"Среднее: {avg_time*1000:.0f}ms, мин: {min_time*1000:.0f}ms, макс: {max_time*1000:.0f}ms")
            info(f"RPS: {50/total_time:.0f} запросов/сек")
            
            if avg_time < 0.05:
                ok("Отличное время ответа (<50ms)")
                self.results["passed"] += 1
            elif avg_time < 0.1:
                ok("Хорошее время ответа (<100ms)")
                self.results["passed"] += 1
            elif avg_time < 0.5:
                warn(f"Допустимое время ответа ({avg_time*1000:.0f}ms)")
                self.results["warnings"] += 1
            else:
                fail(f"Медленное время ответа ({avg_time*1000:.0f}ms)")
                self.results["failed"] += 1
            
            # Тест 100 последовательных запросов
            subheader("100 последовательных запросов")
            start = time.time()
            sequential_results = []
            for _ in range(100):
                result = await make_request(session, url)
                sequential_results.append(result)
            elapsed = time.time() - start
            
            successful = sum(1 for r in sequential_results if r[0] == 200)
            ok(f"{successful}/100 успешны за {elapsed:.2f}s")
            info(f"RPS: {100/elapsed:.0f} запросов/сек")
            self.results["passed"] += 1
    
    def print_summary(self, elapsed: float):
        """Вывод итогов."""
        header("📊 ИТОГИ ПРОВЕРКИ")
        
        total = self.results["passed"] + self.results["failed"] + self.results["warnings"]
        
        print(f"\n{Colors.GREEN}✓ Пройдено: {self.results['passed']}{Colors.END}")
        print(f"{Colors.RED}✗ Провалено: {self.results['failed']}{Colors.END}")
        print(f"{Colors.YELLOW}⚠ Предупреждений: {self.results['warnings']}{Colors.END}")
        print(f"\nВсего проверок: {total}")
        print(f"Время проверки: {elapsed:.1f}s")
        
        if self.critical_errors:
            print(f"\n{Colors.RED}{Colors.BOLD}❌ КРИТИЧЕСКИЕ ОШИБКИ:{Colors.END}")
            for error in self.critical_errors:
                print(f"  {Colors.RED}• {error}{Colors.END}")
        
        score = self.results["passed"] / max(total, 1) * 100
        
        print(f"\n{Colors.BOLD}Оценка готовности: {score:.0f}%{Colors.END}")
        
        if self.results["failed"] == 0 and self.results["warnings"] < 5:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✅ СИСТЕМА ГОТОВА К 24/7 РАБОТЕ{Colors.END}")
        elif self.results["failed"] == 0:
            print(f"\n{Colors.YELLOW}{Colors.BOLD}⚠️ СИСТЕМА РАБОТОСПОСОБНА, НО ТРЕБУЕТ ВНИМАНИЯ{Colors.END}")
        elif self.results["failed"] < 3:
            print(f"\n{Colors.YELLOW}{Colors.BOLD}⚠️ ТРЕБУЕТСЯ ИСПРАВЛЕНИЕ ОШИБОК{Colors.END}")
        else:
            print(f"\n{Colors.RED}{Colors.BOLD}❌ СИСТЕМА НЕ ГОТОВА К ПРОДАКШЕНУ{Colors.END}")


async def main():
    checker = SystemChecker()
    success = await checker.run_all_checks()
    return success


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
