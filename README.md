# Crypto Payment Gateway

Open-source crypto payment gateway for invoices, hosted checkout pages, merchant APIs, persistent user wallets, webhooks, and automated sweeping.

> Security note: never commit a real `.env`, wallet seed, private key, API key, database dump, or runtime SQLite file. Copy `.env.example`, generate fresh secrets, and rotate any credentials that were ever committed before publishing.

## Features

- FastAPI backend with merchant, hosted checkout, public, wallet, and admin routes.
- PostgreSQL persistence with Alembic migrations.
- Redis-backed workers for invoice expiration, webhook dispatch, log polling, persistent wallet polling, and sweeping.
- EVM support for Base, Arbitrum One, BNB Smart Chain, Polygon, Avalanche, and Optimism.
- Solana and TON adapter scaffolding.
- USDT/USDC token configuration in `config/chains.toml`.
- Webhook signing and retry logic.
- Docker Compose stack for local development.

## Repository Layout

```text
.
├── alembic/                 # database migrations
├── config/chains.toml       # chain, token, RPC, and gas settings
├── docs/                    # integration documentation
├── frontend/                # Vite checkout/admin UI
├── scripts/                 # maintenance and smoke-test scripts
├── src/                     # FastAPI app, services, workers, blockchain adapters
├── tests/                   # pytest suite
├── docker-compose.yml       # local PostgreSQL, Redis, API, workers
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

## Requirements

- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Docker and Docker Compose, optional but recommended

## Quick Start

```bash
git clone https://github.com/<owner>/crypto-payment-gateway.git
cd crypto-payment-gateway

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and generate real local secrets

alembic upgrade head
uvicorn src.main:app --reload
```

API docs:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Health check: `http://localhost:8000/health`

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f api
```

Default local endpoints:

- API: `http://127.0.0.1:8123`
- PostgreSQL: `127.0.0.1:5433`
- Redis: `127.0.0.1:6380`

## Workers

Run workers with Docker Compose, or locally:

```bash
python -m src.workers.evm_log_poller
python -m src.workers.persistent_poller
python -m src.workers.webhook_dispatcher
python -m src.workers.unified_sweeper_runner
python -m src.workers.invoice_expirer
```

## Configuration

Runtime settings are loaded from environment variables. Use `.env.example` as a template.

Important variables:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | `development`, `staging`, or `production` |
| `SECRET_KEY` | application signing secret |
| `ADMIN_SECRET_KEY` | admin panel access secret |
| `DATABASE_URL` | async PostgreSQL URL |
| `REDIS_URL` | Redis URL |
| `ENCRYPTION_KEY` | base64 key for encrypting private keys |
| `HD_WALLET_SEED` / `HD_MASTER_SEED` | HD wallet seed material |
| `FUNDER_PRIVATE_KEY` | optional gas funding key |
| `TREASURY_ADDRESS` | default EVM sweep destination |
| `SOLANA_TREASURY_ADDRESS` / `TON_TREASURY_ADDRESS` | non-EVM sweep destinations |
| `CORS_ORIGINS` | comma-separated allowed origins |

Chain and token metadata lives in `config/chains.toml`.

## Development Checks

```bash
ruff check src tests
pytest
```

## Public Release Checklist

- Rotate every secret that was ever committed.
- Confirm `.env`, database files, RPC benchmark output, and IDE files are ignored.
- Run secret scanning on the full git history before making the repository public.
- Set a real repository name, description, topics, and homepage in GitHub.

## License

MIT
