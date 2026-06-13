#!/bin/bash
# Инициализация БД и запуск приложения

set -e

echo "🔄 Инициализация БД..."

# БД файл примонтирован как volume, просто проверяем доступность
if [ ! -w /app/payment_gateway.db ] && [ -f /app/payment_gateway.db ]; then
    echo "⚠️  БД файл существует, но не доступен для записи"
fi

# Запускаем Python инициализацию БД асинхронно
python << 'EOF'
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine

async def init_db():
    """Инициализировать БД"""
    db_url = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///./payment_gateway.db')
    
    # Создаем engine для проверки доступности
    engine = create_async_engine(db_url, echo=False)
    
    try:
        # Пытаемся подключиться
        async with engine.begin() as conn:
            pass
        print("✅ БД доступна")
    except Exception as e:
        print(f"⚠️  БД ошибка (это нормально при первом запуске): {e}")
    finally:
        await engine.dispose()

asyncio.run(init_db())
EOF

echo "✅ БД готова"

# Если передана команда - выполняем её, иначе запускаем uvicorn
if [ $# -gt 0 ]; then
    echo "🚀 Запуск команды: $@"
    exec "$@"
else
    echo "🚀 Запуск приложения на порту 8000..."
    exec uvicorn src.main:app --host 0.0.0.0 --port 8000
fi
