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

### Docker Compose для локальной разработки

```bash
docker compose up -d
```

Это локальный development workflow. Он не является production-инструкцией и не должен использоваться для ограниченной runtime-роли.

### Ограниченная runtime-роль в production

Для ограниченной runtime-роли требуется актуальный Docker Compose с поддержкой Compose-spec `!override` (проверяйте `docker compose version`; классический `docker-compose` v1 не поддерживается). Runtime service получает явный защищённый `RUNTIME_ENV_FILE`; базовый `.env`/owner connection не наследуется. Production запуск использует базовый Compose, `docker-compose.runtime-role.yml`, защищённый immutable-image override, точное имя одного сервиса и `--no-build --no-deps`. Runtime image никогда не выполняет миграции. Полная каноническая инструкция: [`docs/runtime-deployment.md`](docs/runtime-deployment.md).

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
