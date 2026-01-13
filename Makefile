.PHONY: install dev test lint format migrate up down logs clean help

# Default target
help:
	@echo "Arbitron Payment Gateway - Makefile Commands"
	@echo ""
	@echo "Setup:"
	@echo "  install       Install production dependencies"
	@echo "  dev           Install development dependencies"
	@echo ""
	@echo "Database (PostgreSQL):"
	@echo "  db-start      Start PostgreSQL container"
	@echo "  db-stop       Stop PostgreSQL container"
	@echo "  migrate       Run database migrations"
	@echo "  migrate-new   Create new migration (NAME=migration_name)"
	@echo "  migrate-down  Rollback last migration"
	@echo ""
	@echo "Docker:"
	@echo "  up            Start all services with docker-compose"
	@echo "  down          Stop all services"
	@echo "  logs          Show logs from all services"
	@echo "  build         Build docker images"
	@echo ""
	@echo "Development:"
	@echo "  run           Run API server locally"
	@echo "  worker-poller Run EVM poller worker locally"
	@echo "  worker-webhook Run webhook dispatcher locally"
	@echo "  worker-sweeper Run sweeper worker locally"
	@echo "  worker-expirer Run invoice expirer locally"
	@echo ""
	@echo "Code Quality:"
	@echo "  lint          Run linters (ruff)"
	@echo "  format        Format code (ruff format)"
	@echo "  test          Run tests"
	@echo "  test-cov      Run tests with coverage"
	@echo ""
	@echo "Utilities:"
	@echo "  clean         Remove cache and build files"
	@echo "  gen-key       Generate encryption key"
	@echo "  gen-seed      Generate HD wallet seed"

# ========================================
# Setup
# ========================================

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements.txt
	pip install pytest pytest-asyncio pytest-cov ruff mypy

# ========================================
# Database
# ========================================

db-start:
	docker-compose up -d postgres
	@echo "Waiting for PostgreSQL to be ready..."
	@sleep 3
	docker-compose exec postgres pg_isready -U arbitron -d arbitron_payment

db-stop:
	docker-compose stop postgres

migrate:
	alembic upgrade head

migrate-new:
	@if [ -z "$(NAME)" ]; then echo "Usage: make migrate-new NAME=migration_name"; exit 1; fi
	alembic revision --autogenerate -m "$(NAME)"

migrate-down:
	alembic downgrade -1

# ========================================
# Docker
# ========================================

up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

build:
	docker-compose build

# ========================================
# Development
# ========================================

run:
	python -m src.main

cli:
	python run_cli.py

worker-poller:
	python -m src.workers.evm_log_poller

worker-webhook:
	python -m src.workers.webhook_dispatcher

worker-sweeper:
	python -m src.workers.sweeper

worker-expirer:
	python -m src.workers.invoice_expirer

# ========================================
# Code Quality
# ========================================

lint:
	ruff check src/

format:
	ruff format src/
	ruff check --fix src/

test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=src --cov-report=html --cov-report=term-missing

test-unit:
	pytest tests/test_*.py -v --ignore=tests/test_integration.py --ignore=tests/test_e2e.py

test-integration:
	pytest tests/test_integration.py tests/test_e2e.py -v

test-all:
	pytest tests/ -v --tb=long

simulate:
	python3 scripts/simulate_light.py

simulate-full:
	python3 scripts/simulate_payment.py

# ========================================
# Utilities
# ========================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .coverage htmlcov/ dist/ build/

gen-key:
	@python -c "import secrets, base64; print('ENCRYPTION_KEY=' + base64.b64encode(secrets.token_bytes(32)).decode())"

gen-seed:
	@python -c "from mnemonic import Mnemonic; print('HD_WALLET_SEED=' + Mnemonic('english').generate(256))"
