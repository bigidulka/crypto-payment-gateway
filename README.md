# Arbitron Payment Gateway

Криптовалютный платёжный шлюз с поддержкой Base, Arbitrum, BSC и токенов USDT/USDC.

## Быстрый старт

### Требования

- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Docker & Docker Compose (опционально)

### Установка

```bash
# Клонирование и установка зависимостей
cd arbitron-payment
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Копирование конфига
cp .env.example .env
# Отредактируйте .env с вашими настройками

# Миграции БД
alembic upgrade head

# Запуск API сервера
uvicorn src.main:app --reload

# Запуск воркеров (в отдельных терминалах)
arq src.workers.evm_log_poller.WorkerSettings
arq src.workers.webhook_dispatcher.WorkerSettings
arq src.workers.sweeper.WorkerSettings
```

### Docker Compose

```bash
docker compose up -d
```

## Архитектура

```
┌─────────────────┐     ┌─────────────────┐
│   FastAPI App   │     │  Worker Service │
│   (Merchant +   │     │  - Log Poller   │
│    Hosted API)  │     │  - Webhooks     │
└────────┬────────┘     │  - Sweeper      │
         │              └────────┬────────┘
         │                       │
    ┌────┴───────────────────────┴────┐
    │           PostgreSQL            │
    │              Redis              │
    └─────────────────────────────────┘
                   │
    ┌──────────────┴──────────────┐
    │   Base / Arbitrum / BSC     │
    │        (EVM RPC)            │
    └─────────────────────────────┘
```

## API Документация

После запуска доступно:

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Конфигурация

Все настройки через переменные окружения (см. `.env.example`):

| Переменная       | Описание                                            |
| ---------------- | --------------------------------------------------- |
| `DATABASE_URL`   | PostgreSQL connection string                        |
| `REDIS_URL`      | Redis connection string                             |
| `ENCRYPTION_KEY` | 32-byte base64 ключ для шифрования приватных ключей |
| `HD_WALLET_SEED` | BIP39 мнемоника для HD кошелька                     |
| `BASE_RPC_URL`   | RPC endpoint для Base                               |
| `ARB_RPC_URL`    | RPC endpoint для Arbitrum                           |
| `BSC_RPC_URL`    | RPC endpoint для BSC                                |

## Лицензия

MIT
