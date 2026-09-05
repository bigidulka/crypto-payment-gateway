"""Compatibility rehearsal: legacy merchant invoice API against additive schema 0010.

No provider adapter, RPC scanner, worker, payout, or external network request is
invoked. The actual application lifespan runs while its external dependency
hooks are mocked; endpoint sessions use a local transaction-bound PostgreSQL
session.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import src.db.redis as redis_module
import src.db.session as session_module
import src.main as main_module
from src.api.deps import get_session
from src.core.config import get_settings
from src.core.security import hash_api_key
from src.db.models import ApiKey, Merchant, OutboxWebhook, Webhook


@pytest_asyncio.fixture
async def api_compat_session() -> AsyncSession:
    database_url = os.getenv("LEDGER_API_COMPAT_DATABASE_URL")
    if not database_url:
        pytest.fail("LEDGER_API_COMPAT_DATABASE_URL must point to a dedicated 0010 database")
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "::1"}:
        pytest.fail("LEDGER_API_COMPAT_DATABASE_URL must use a loopback postgresql+asyncpg URL")
    if not parsed.database or not parsed.database.startswith("test_"):
        pytest.fail("LEDGER_API_COMPAT_DATABASE_URL database must start with test_")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0010_ledger_foundation":
                pytest.fail(
                    "LEDGER_API_COMPAT_DATABASE_URL must be migrated to 0010_ledger_foundation"
                )
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_invoice_endpoints_remain_compatible_on_0010(
    api_compat_session: AsyncSession,
    monkeypatch,
):
    redis_url = os.getenv("LEDGER_API_COMPAT_REDIS_URL")
    if not redis_url:
        pytest.fail("LEDGER_API_COMPAT_REDIS_URL must point to dedicated local Redis")
    redis_host = redis_url.split("//", 1)[-1].rsplit("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if redis_host not in {"127.0.0.1", "::1"}:
        pytest.fail("LEDGER_API_COMPAT_REDIS_URL must use a loopback Redis URL")
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("DATABASE_URL", os.environ["LEDGER_API_COMPAT_DATABASE_URL"])
    get_settings.cache_clear()
    session_module.get_engine.cache_clear()
    session_module.get_session_factory.cache_clear()
    redis_module._redis_pool = None

    async def no_external_operation(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_module, "init_rpc_managers", no_external_operation)
    monkeypatch.setattr(main_module, "sync_deposit_address_counter", no_external_operation)
    monkeypatch.setattr(main_module, "close_all_rpc_managers", no_external_operation)
    monkeypatch.setattr(main_module, "close_redis", no_external_operation)
    monkeypatch.setattr(main_module, "close_db", no_external_operation)
    monkeypatch.setattr(main_module.limiter, "enabled", False)

    raw_key = "compatibility-local-key"
    merchant = Merchant(
        name="0010 compatibility merchant",
        email=f"compat-{uuid.uuid4()}@test.invalid",
    )
    api_compat_session.add(merchant)
    await api_compat_session.flush()
    api_compat_session.add(
        ApiKey(
            merchant_id=merchant.id,
            key_hash=hash_api_key(raw_key),
            key_prefix=raw_key[:8],
            name="compatibility",
        )
    )
    await api_compat_session.flush()
    api_compat_session.add(
        Webhook(
            merchant_id=merchant.id,
            url="https://example.test/webhook",
            secret="compatibility-webhook-secret",
            events=["invoice.created"],
        )
    )
    await api_compat_session.flush()

    app = main_module.create_app()

    async def override_session():
        yield api_compat_session

    app.dependency_overrides[get_session] = override_session
    try:
        async with main_module.lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://compat.test"
            ) as client:
                health = await client.get("/health")
                ready = await client.get("/ready")
                assert health.status_code == 200
                assert ready.status_code == 200
                assert health.json()["status"] == "healthy"
                assert ready.json()["status"] == "ready"

                request = {
                    "amount": "12.34",
                    "asset": "USDC",
                    "allowed_chains": ["base"],
                    "ttl_minutes": 60,
                    "metadata": {"compatibility": True},
                }
                headers = {
                    "Authorization": f"Bearer {raw_key}",
                    "Idempotency-Key": "compatibility-invoice",
                }
                created = await client.post("/v1/invoices", json=request, headers=headers)
                assert created.status_code == 201
                created_body = created.json()

                repeated = await client.post("/v1/invoices", json=request, headers=headers)
                assert repeated.status_code == 201
                assert repeated.json()["id"] == created_body["id"]

                fetched = await client.get(f"/v1/invoices/{created_body['id']}", headers=headers)
                assert fetched.status_code == 200
                assert fetched.json()["id"] == created_body["id"]

                listed = await client.get("/v1/invoices", headers=headers)
                assert listed.status_code == 200
                assert listed.json()["total"] == 1
                assert (
                    await api_compat_session.scalar(select(func.count()).select_from(OutboxWebhook))
                    == 1
                )
    finally:
        await redis_module.close_redis()
        await session_module.close_db()
        session_module.get_engine.cache_clear()
        session_module.get_session_factory.cache_clear()
        get_settings.cache_clear()
        app.dependency_overrides.clear()
