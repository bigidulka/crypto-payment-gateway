#!/usr/bin/env python3
"""
Скрипт миграции данных из SQLite в PostgreSQL.

Использование:
    python scripts/migrate_sqlite_to_postgres.py

Требования:
    1. PostgreSQL должен быть запущен (docker-compose up -d postgres)
    2. Миграции alembic должны быть применены (alembic upgrade head)
    3. SQLite база должна существовать в data/arbitron_payment.db
"""

import asyncio
import sqlite3
from pathlib import Path

import asyncpg

# Конфигурация
SQLITE_PATH = Path(__file__).parent.parent / "data" / "arbitron_payment.db"
POSTGRES_DSN = "postgresql://arbitron:arbitron_secure_password@localhost:5432/arbitron_payment"

# Таблицы в порядке зависимостей (родительские сначала)
TABLES_ORDER = [
    "merchants",
    "invoices", 
    "deposit_addresses",
    "payment_sessions",
    "onchain_txs",
    "webhook_events",
    "sweep_tasks",
    "user_wallets",
    "user_wallet_deposits",
]


def get_sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    """Получить список таблиц в SQLite."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
    )
    return [row[0] for row in cursor.fetchall()]


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Получить список колонок таблицы."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


async def copy_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn: asyncpg.Connection,
    table: str
) -> int:
    """Копировать данные из SQLite таблицы в PostgreSQL."""
    columns = get_table_columns(sqlite_conn, table)
    
    if not columns:
        print(f"  ⚠️  Таблица {table} не найдена в SQLite")
        return 0
    
    # Читаем все данные из SQLite
    cursor = sqlite_conn.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    
    if not rows:
        print(f"  ⏭️  Таблица {table}: пустая")
        return 0
    
    # Формируем INSERT запрос
    columns_str = ", ".join(f'"{col}"' for col in columns)
    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
    
    insert_sql = f'INSERT INTO {table} ({columns_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
    
    # Вставляем данные
    count = 0
    for row in rows:
        # Конвертируем None и другие типы
        converted_row = []
        for val in row:
            if isinstance(val, bytes):
                converted_row.append(val)
            else:
                converted_row.append(val)
        
        try:
            await pg_conn.execute(insert_sql, *converted_row)
            count += 1
        except Exception as e:
            print(f"    ⚠️  Ошибка вставки в {table}: {e}")
            # Продолжаем с следующей записью
    
    print(f"  ✅ Таблица {table}: {count}/{len(rows)} записей")
    return count


async def reset_sequences(pg_conn: asyncpg.Connection, table: str):
    """Сбросить sequence для таблицы на максимальный ID + 1."""
    try:
        # Проверяем есть ли колонка id
        result = await pg_conn.fetchval(
            f"SELECT MAX(id) FROM {table}"
        )
        if result:
            # Находим sequence
            seq_name = f"{table}_id_seq"
            await pg_conn.execute(f"SELECT setval('{seq_name}', {result})")
    except Exception:
        pass  # Таблица может не иметь id или sequence


async def main():
    print("=" * 60)
    print("Миграция данных SQLite → PostgreSQL")
    print("=" * 60)
    
    # Проверяем SQLite
    if not SQLITE_PATH.exists():
        print(f"❌ SQLite база не найдена: {SQLITE_PATH}")
        return 1
    
    print(f"\n📂 SQLite: {SQLITE_PATH}")
    print(f"🐘 PostgreSQL: {POSTGRES_DSN.split('@')[1]}")
    
    # Подключаемся к SQLite
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    
    # Получаем таблицы из SQLite
    sqlite_tables = get_sqlite_tables(sqlite_conn)
    print(f"\n📋 Таблицы в SQLite: {', '.join(sqlite_tables)}")
    
    # Подключаемся к PostgreSQL
    try:
        pg_conn = await asyncpg.connect(POSTGRES_DSN)
    except Exception as e:
        print(f"\n❌ Не удалось подключиться к PostgreSQL: {e}")
        print("\n💡 Убедитесь что:")
        print("   1. PostgreSQL запущен: docker-compose up -d postgres")
        print("   2. Миграции применены: alembic upgrade head")
        return 1
    
    print("\n🔄 Начинаем копирование данных...\n")
    
    total_count = 0
    
    # Копируем таблицы в порядке зависимостей
    for table in TABLES_ORDER:
        if table in sqlite_tables:
            count = await copy_table(sqlite_conn, pg_conn, table)
            total_count += count
            await reset_sequences(pg_conn, table)
    
    # Копируем оставшиеся таблицы
    for table in sqlite_tables:
        if table not in TABLES_ORDER:
            count = await copy_table(sqlite_conn, pg_conn, table)
            total_count += count
            await reset_sequences(pg_conn, table)
    
    # Закрываем соединения
    sqlite_conn.close()
    await pg_conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"✅ Миграция завершена! Скопировано записей: {total_count}")
    print(f"{'=' * 60}")
    
    print("\n📝 Следующие шаги:")
    print("   1. Проверьте данные в PostgreSQL")
    print("   2. Обновите DATABASE_URL в .env")
    print("   3. Перезапустите сервисы: docker-compose up -d")
    
    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
