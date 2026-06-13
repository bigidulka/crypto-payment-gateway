#!/usr/bin/env python3
"""
Migrate data from SQLite to PostgreSQL.
Run inside docker container with both DBs accessible.
"""

import asyncio
import sqlite3
import os
import json
from datetime import datetime

import asyncpg


SQLITE_PATH = "/data/payment_gateway.db"
POSTGRES_DSN = os.getenv("DATABASE_URL", "").replace("+asyncpg", "").replace("postgresql", "postgres")

# Tables in dependency order
TABLES = [
    "merchants",
    "api_keys",
    "webhooks",
    "invoices",
    "deposit_addresses",
    "payment_sessions",
    "onchain_txs",
    "invoice_events",
    "outbox_webhooks",
    "sweep_jobs",
    "chain_checkpoints",
    "user_wallets",
    "wallet_addresses",
    "deposits",
    "user_balances",
]

# Columns that contain ARRAY stored as JSON string in SQLite
ARRAY_COLUMNS = {
    'invoices': ['allowed_chains'],
    'webhooks': ['events'],
}

# Enum columns that need UPPERCASE->lowercase conversion
# Some PostgreSQL enums are lowercase while SQLite stored them as UPPERCASE
ENUM_COLUMNS = {
    # invoice_status is UPPERCASE in PostgreSQL - no conversion
    'deposits': ['status'],          # deposit_status is lowercase
    'onchain_txs': ['status'],       # tx_status is lowercase
    'sweep_jobs': ['state'],         # sweep_state is lowercase (PENDING_GAS -> pending_gas)
}

# JSON columns
JSON_COLUMNS = {
    'invoices': ['extra_data'],
}


def convert_value(val, col_name, table_name, pg_type=None):
    """Convert SQLite value to PostgreSQL compatible value."""
    if val is None:
        return None
    
    # Handle ARRAY columns (stored as JSON strings in SQLite)
    array_cols = ARRAY_COLUMNS.get(table_name, [])
    if col_name in array_cols:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return [val] if val else []
        return val if isinstance(val, list) else []
    
    # Handle ENUM columns (UPPERCASE -> lowercase)
    enum_cols = ENUM_COLUMNS.get(table_name, [])
    if col_name in enum_cols:
        if isinstance(val, str):
            return val.lower()
        return val
    
    # Handle JSON columns
    json_cols = JSON_COLUMNS.get(table_name, [])
    if col_name in json_cols:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return None
        return val
    
    # Convert datetime strings
    if isinstance(val, str) and pg_type and 'timestamp' in pg_type:
        for fmt in [
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ]:
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    
    # Convert SQLite boolean (0/1) to Python bool
    if pg_type == 'boolean' and isinstance(val, int):
        return bool(val)
    
    return val


async def main():
    print("=" * 60)
    print("Migrating data from SQLite to PostgreSQL")
    print("=" * 60)
    
    # Connect to SQLite
    if not os.path.exists(SQLITE_PATH):
        print(f"❌ SQLite not found: {SQLITE_PATH}")
        return 1
    
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    
    # Get available tables from SQLite
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
    )
    sqlite_tables = [row[0] for row in cursor.fetchall()]
    print(f"\n📋 SQLite tables: {', '.join(sqlite_tables)}")
    
    # Connect to PostgreSQL
    pg_dsn = POSTGRES_DSN
    if not pg_dsn:
        pg_dsn = "postgres://gateway:payment_gateway_password@postgres:5432/payment_gateway"
    
    print(f"🐘 Connecting to PostgreSQL...")
    try:
        pg_conn = await asyncpg.connect(pg_dsn)
    except Exception as e:
        print(f"❌ PostgreSQL connection failed: {e}")
        return 1
    
    print("✅ Connected\n")
    
    # Get PostgreSQL column types
    pg_columns = {}
    for table in TABLES:
        try:
            result = await pg_conn.fetch(
                """
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = $1
                """,
                table
            )
            pg_columns[table] = {r['column_name']: r['data_type'] for r in result}
        except:
            pg_columns[table] = {}
    
    total = 0
    
    for table in TABLES:
        if table not in sqlite_tables:
            continue
        
        # Get SQLite columns
        cursor = sqlite_conn.execute(f"PRAGMA table_info({table})")
        sqlite_cols = [row[1] for row in cursor.fetchall()]
        
        # Get matching PostgreSQL columns
        pg_table_cols = pg_columns.get(table, {})
        
        # Only use columns that exist in both
        common_cols = [c for c in sqlite_cols if c in pg_table_cols]
        
        if not common_cols:
            print(f"⏭️  {table}: no matching columns")
            continue
        
        # Get data
        cursor = sqlite_conn.execute(f"SELECT {', '.join(common_cols)} FROM {table}")
        rows = cursor.fetchall()
        
        if not rows:
            print(f"⏭️  {table}: empty")
            continue
        
        # Build INSERT
        cols_str = ", ".join(f'"{c}"' for c in common_cols)
        placeholders = ", ".join(f"${i+1}" for i in range(len(common_cols)))
        insert_sql = f'INSERT INTO "{table}" ({cols_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
        
        count = 0
        errors = 0
        for row in rows:
            # Convert values
            converted = []
            for i, val in enumerate(row):
                col_name = common_cols[i]
                pg_type = pg_table_cols.get(col_name, '')
                converted.append(convert_value(val, col_name, table, pg_type))
            
            try:
                await pg_conn.execute(insert_sql, *converted)
                count += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  ⚠️  {table} error: {e}")
        
        total += count
        if errors > 3:
            print(f"✅ {table}: {count}/{len(rows)} rows ({errors} errors)")
        else:
            print(f"✅ {table}: {count}/{len(rows)} rows")
        
        # Reset sequence
        try:
            max_id = await pg_conn.fetchval(f'SELECT MAX(id) FROM "{table}"')
            if max_id:
                await pg_conn.execute(f"SELECT setval('{table}_id_seq', $1)", max_id)
        except:
            pass
    
    sqlite_conn.close()
    await pg_conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"✅ Migration complete! Total rows: {total}")
    print(f"{'=' * 60}")
    
    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
