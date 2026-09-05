"""
Тесты слоя рельсов: CryptoBot-адаптер, шифрование кредов, реестр.
"""

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("ENCRYPTION_KEY", "hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=")

from src.crypto.encryption import decrypt_secret, encrypt_secret  # noqa: E402
from src.db.models.base import UniversalJSON  # noqa: E402
from src.payments.rails.base import (  # noqa: E402
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayoutRequest,
    RailType,
)
from src.payments.rails.cryptobot import CryptobotRail  # noqa: E402
from src.payments.rails.registry import (  # noqa: E402
    build_rail,
    encrypt_credentials,
)

# === шифрование секретов ===


def test_secret_roundtrip():
    creds = {"token": "secret-token", "app": 12345}
    blob = encrypt_secret(json.dumps(creds), os.environ["ENCRYPTION_KEY"])
    assert decrypt_secret(blob, os.environ["ENCRYPTION_KEY"]) == json.dumps(creds)


def test_wrong_key_fails():
    blob = encrypt_secret("x", os.environ["ENCRYPTION_KEY"])
    with pytest.raises(ValueError):
        decrypt_secret(blob, "AQ" + "A" * 42)  # другой 32-байтный ключ


def test_secret_rejects_noncanonical_encryption_key():
    with pytest.raises(ValueError, match="valid base64"):
        encrypt_secret("x", "!" * 44)


def test_secret_rejects_non_bytes_ciphertext():
    with pytest.raises(ValueError, match="too short"):
        decrypt_secret("not-bytes", os.environ["ENCRYPTION_KEY"])


def test_secret_rejects_non_string_plaintext():
    with pytest.raises(TypeError, match="secret must be a string"):
        encrypt_secret(b"not-text", os.environ["ENCRYPTION_KEY"])


def test_rail_assets_use_actual_postgresql_json_type():
    from sqlalchemy.dialects import postgresql

    from src.db.models.rail import Rail

    assert isinstance(Rail.__table__.c.assets.type, UniversalJSON)
    assert isinstance(
        Rail.__table__.c.assets.type.load_dialect_impl(postgresql.dialect()),
        postgresql.JSON,
    )


@pytest.mark.parametrize(
    "assets, error",
    [
        ({"USDT": True}, "non-empty list"),
        ("USDT", "non-empty list"),
        ([], "non-empty list"),
        (["USDT", "USDT"], "duplicates"),
        (["usdt"], "trimmed uppercase"),
        (["USDT", " "], "empty symbols"),
        (["USDT", 1], "only strings"),
    ],
)
def test_rail_assets_reject_malformed_values(assets, error):
    from src.db.models.rail import Rail

    with pytest.raises(ValueError, match=error):
        Rail(merchant_id=uuid.uuid4(), rail_type="cryptobot", assets=assets)


def test_rail_assets_accept_canonical_symbols():
    from src.db.models.rail import Rail

    rail = Rail(merchant_id=uuid.uuid4(), rail_type="cryptobot", assets=["USDT", "USDC"])
    assert rail.assets == ["USDT", "USDC"]


@pytest.mark.asyncio
async def test_rail_assets_roundtrip_against_isolated_postgres():
    """Run only when an isolated PostgreSQL URL is explicitly supplied."""
    database_url = os.getenv("ISOLATED_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("set ISOLATED_POSTGRES_DATABASE_URL for PostgreSQL ORM roundtrip")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models.merchant import Merchant
    from src.db.models.rail import Rail

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            merchant = Merchant(name="Rail ORM test", email=f"rail-{uuid.uuid4().hex}@example.test")
            rail = Rail(merchant=merchant, rail_type="cryptobot", assets=["USDT", "USDC"])
            session.add_all([merchant, rail])
            await session.commit()
            await session.refresh(rail)
            assert rail.assets == ["USDT", "USDC"]

            data_type = await session.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'rails' "
                    "AND column_name = 'assets'"
                )
            )
            assert data_type == "json"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rail_assets_omitted_default_is_rejected_at_postgres_flush():
    """A client-side default must not persist an empty JSON asset list."""
    database_url = os.getenv("ISOLATED_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("set ISOLATED_POSTGRES_DATABASE_URL for PostgreSQL flush validation")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models.merchant import Merchant
    from src.db.models.rail import Rail

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            merchant = Merchant(
                name="Rail default test",
                email=f"rail-{uuid.uuid4().hex}@example.test",
            )
            rail = Rail(merchant=merchant, rail_type="cryptobot")
            session.add(rail)
            with pytest.raises(ValueError, match="non-empty list"):
                await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rail_assets_in_place_mutation_is_rejected_at_postgres_flush():
    """MutableList.clear cannot bypass the canonical asset contract."""
    database_url = os.getenv("ISOLATED_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("set ISOLATED_POSTGRES_DATABASE_URL for PostgreSQL flush validation")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models.merchant import Merchant
    from src.db.models.rail import Rail

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            merchant = Merchant(
                name="Rail mutation test",
                email=f"rail-{uuid.uuid4().hex}@example.test",
            )
            rail = Rail(merchant=merchant, rail_type="cryptobot", assets=["USDT"])
            session.add(rail)
            await session.commit()
            rail.assets.clear()
            with pytest.raises(ValueError, match="non-empty list"):
                await session.flush()
    finally:
        await engine.dispose()


# === CryptoBot против фейкового API ===


@pytest.fixture
async def cryptobot_api():
    seen = []

    async def create_invoice(request):
        seen.append(
            {
                "token": request.headers.get("Crypto-Pay-API-Token"),
                "body": await request.json(),
            }
        )
        body = seen[-1]["body"]
        if body["amount"] == "2":
            result = {
                "invoice_id": 4242,
                "status": "active",
                "bot_invoice_url": "https://t.me/CryptoBot?start=I4242",
                "asset": "USDT",
                "amount": "3",
            }
        else:
            result = {
                "invoice_id": 4242,
                "status": "active",
                "bot_invoice_url": "https://t.me/CryptoBot?start=I4242",
                "asset": "USDT",
                "amount": "10.5",
            }
        return web.json_response({"ok": True, "result": result})

    async def get_invoices(request):
        invoice_id = request.query.get("invoice_ids")
        if invoice_id == "mismatch":
            items = [{"invoice_id": 999, "status": "paid", "asset": "USDT", "amount": "10"}]
        elif invoice_id == "ambiguous":
            items = [
                {"invoice_id": "ambiguous", "status": "paid", "asset": "USDT", "amount": "10"},
                {"invoice_id": "ambiguous", "status": "paid", "asset": "USDT", "amount": "10"},
            ]
        elif invoice_id == "unknown-status":
            items = [
                {"invoice_id": invoice_id, "status": "processing", "asset": "USDT", "amount": "10"}
            ]
        elif invoice_id == "nan":
            items = [{"invoice_id": invoice_id, "status": "paid", "asset": "USDT", "amount": "NaN"}]
        else:
            items = [
                {
                    "invoice_id": 4242,
                    "status": "paid",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=I4242",
                    "asset": "USDT",
                    "amount": "10.5",
                }
            ]
        return web.json_response({"ok": True, "result": {"items": items}})
    async def create_check(request):
        body = await request.json()
        seen.append({"check": body})
        if body["amount"] == "6":
            result = {"bot_check_url": "https://t.me/CryptoBot?start=Cmissing"}
        elif body["amount"] == "7":
            result = {"check_id": 77}
        elif body["amount"] == "8":
            return web.Response(text="not json", status=200, content_type="text/plain")
        else:
            result = {
                "check_id": 99,
                "bot_check_url": "https://t.me/CryptoBot?start=C99",
            }
        return web.json_response({"ok": True, "result": result})

    async def reject(request):
        return web.json_response(
            {"ok": False, "error": {"code": 401, "name": "AUTH_FAILED"}}, status=401
        )

    async def rate_limited(_request):
        return web.json_response(
            {"ok": False, "error": "too many requests"},
            status=429,
            headers={"Retry-After": "17"},
        )

    async def bad_http_success(_request):
        return web.json_response({"ok": True, "result": {}}, status=429)

    async def nonliteral_ok(_request):
        return web.json_response({"ok": 1, "result": {}}, status=200)

    async def malformed(_request):
        return web.Response(text="not json", status=502, content_type="text/plain")

    app = web.Application()
    app.router.add_post("/api/createInvoice", create_invoice)
    app.router.add_get("/api/getInvoices", get_invoices)
    app.router.add_post("/api/createCheck", create_check)
    app.router.add_post("/api/boom", reject)
    app.router.add_post("/api/rate-limited", rate_limited)
    app.router.add_post("/api/bad-http-success", bad_http_success)
    app.router.add_post("/api/nonliteral-ok", nonliteral_ok)
    app.router.add_post("/api/malformed", malformed)

    server = TestServer(app)
    await server.start_server()
    try:
        yield SimpleNamespace(url=str(server.make_url("/api")), seen=seen)
    finally:
        await server.close()


async def test_create_invoice_passes_token_and_payload(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        inv = await rail.create_invoice(
            RailInvoiceRequest(
                amount=Decimal("10.5"),
                asset="usdt",
                description="test",
                external_id="PAY_abc",
                expires_in_minutes=30,
            )
        )
    finally:
        await rail.close()

    assert inv.provider_invoice_id == "4242"
    assert inv.status == RailInvoiceStatus.PENDING
    assert inv.pay_url.startswith("https://t.me/CryptoBot")
    call = cryptobot_api.seen[0]
    assert call["token"] == "tok-1"
    assert call["body"]["asset"] == "USDT"
    assert call["body"]["expires_in"] == 1800
    assert call["body"]["payload"] == "PAY_abc"


async def test_verify_payment_returns_amount_when_paid(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        payment = await rail.verify_payment("4242")
    finally:
        await rail.close()
    assert payment is not None
    assert payment.amount == Decimal("10.5")
    assert payment.asset == "USDT"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "0", "-1"])
async def test_create_invoice_rejects_nonfinite_or_nonpositive_amount_without_call(
    cryptobot_api, amount
):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="positive and finite"):
            await rail.create_invoice(RailInvoiceRequest(amount=Decimal(amount), asset="USDT"))
    finally:
        await rail.close()
    assert cryptobot_api.seen == []


async def test_create_invoice_rejects_provider_amount_mismatch_as_uncertain(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="amount mismatch") as exc:
            await rail.create_invoice(RailInvoiceRequest(amount=Decimal("2"), asset="USDT"))
    finally:
        await rail.close()
    assert exc.value.outcome_unknown


@pytest.mark.parametrize("provider_invoice_id", ["unknown-status", "nan"])
async def test_get_invoice_rejects_unknown_status_or_nonfinite_amount(
    cryptobot_api, provider_invoice_id
):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError):
            await rail.get_invoice(provider_invoice_id)
    finally:
        await rail.close()


async def test_payout_creates_check(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        payout = await rail.payout(
            RailPayoutRequest(amount=Decimal("5"), asset="USDT", destination="12345")
        )
    finally:
        await rail.close()
    assert payout.success
    assert payout.check_url.endswith("C99")
    assert cryptobot_api.seen[-1]["check"]["pin_to_user_id"] == 12345


async def test_create_invoice_rejects_unsupported_asset_without_provider_call(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="unsupported asset"):
            await rail.create_invoice(RailInvoiceRequest(amount=Decimal("1"), asset="DOGE"))
    finally:
        await rail.close()
    assert cryptobot_api.seen == []


@pytest.mark.parametrize("provider_invoice_id", ["mismatch", "ambiguous"])
async def test_get_invoice_rejects_missing_or_ambiguous_provider_id(
    cryptobot_api, provider_invoice_id
):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="missing or ambiguous"):
            await rail.get_invoice(provider_invoice_id)
    finally:
        await rail.close()


@pytest.mark.parametrize("amount", ["6", "7"])
async def test_payout_rejects_missing_provider_reference(cryptobot_api, amount):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        request = RailPayoutRequest(amount=Decimal(amount), asset="USDT", destination="12345")
        with pytest.raises(RailError, match="lacks check id or URL") as exc:
            await rail.payout(request)
    finally:
        await rail.close()
    assert exc.value.outcome_unknown


async def test_payout_requires_pinned_numeric_recipient(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="positive numeric Telegram recipient"):
            await rail.payout(RailPayoutRequest(amount=Decimal("5"), asset="USDT", destination=""))
    finally:
        await rail.close()


@pytest.mark.parametrize("destination", ["", "0", "-1", "١٢٣", "１２３"])
async def test_payout_rejects_non_ascii_or_nonpositive_recipient(cryptobot_api, destination):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    request = RailPayoutRequest(amount=Decimal("5"), asset="USDT", destination=destination)
    try:
        with pytest.raises(RailError, match="positive numeric Telegram recipient"):
            await rail.payout(request)
    finally:
        await rail.close()


async def test_rate_limit_is_retryable_without_adapter_retry(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError) as exc:
            await rail._api("POST", "rate-limited", {})
    finally:
        await rail.close()
    assert not exc.value.permanent
    assert exc.value.retry_after_seconds == 17


@pytest.mark.parametrize("endpoint", ["bad-http-success", "nonliteral-ok"])
async def test_http_status_and_literal_ok_are_both_required(cryptobot_api, endpoint):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError):
            await rail._api("POST", endpoint, {})
    finally:
        await rail.close()


async def test_malformed_http_error_is_rail_error(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError, match="malformed JSON") as exc:
            await rail._api("POST", "malformed", {})
    finally:
        await rail.close()
    assert not exc.value.permanent
    assert not exc.value.outcome_unknown


async def test_mutating_malformed_success_is_outcome_unknown(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        request = RailPayoutRequest(amount=Decimal("8"), asset="USDT", destination="12345")
        with pytest.raises(RailError, match="malformed JSON") as exc:
            await rail.payout(request)
    finally:
        await rail.close()
    assert exc.value.outcome_unknown


def test_cryptobot_testnet_uses_testnet_endpoint_and_rejects_unknown_network():
    from src.payments.rails.base import RailError
    from src.payments.rails.cryptobot import TESTNET_API_BASE

    assert CryptobotRail("tok", network="TEST_NET")._base_url == TESTNET_API_BASE
    with pytest.raises(RailError, match="unsupported network"):
        CryptobotRail("tok", network="unknown")

async def test_api_error_is_permanent_for_auth(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("bad", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError) as exc:
            await rail._api("POST", "boom", {})
    finally:
        await rail.close()
    assert exc.value.permanent


def test_rail_requires_token():
    from src.payments.rails.base import RailError

    with pytest.raises(RailError):
        CryptobotRail("")


# === реестр ===


def test_registry_builds_cryptobot_with_decrypted_credentials(monkeypatch):
    from src.core.config import Settings
    from src.payments.rails import registry

    encrypted = encrypt_credentials({"token": "tok-9"})
    monkeypatch.setattr(registry, "get_settings", lambda: Settings())
    rail = build_rail("cryptobot", encrypted)
    assert isinstance(rail, CryptobotRail)
    assert rail.rail_type == RailType.CRYPTOBOT


@pytest.mark.parametrize(
    "credentials",
    [
        {"token": "tok", "base_url": "https://evil.invalid"},
        {"token": "tok", "network": "TEST_NET"},
    ],
)
def test_registry_rejects_reserved_credential_overrides(monkeypatch, credentials):
    from src.core.config import Settings
    from src.payments.rails import registry

    encrypted = encrypt_credentials(credentials)
    monkeypatch.setattr(registry, "get_settings", lambda: Settings())
    with pytest.raises(ValueError, match="forbidden keys"):
        build_rail("cryptobot", encrypted)


def test_registry_rejects_unknown_rail():
    with pytest.raises(ValueError, match="unsupported or unregistered"):
        build_rail("p2p_cash", None)


def test_registry_rejects_known_but_unregistered_rail():
    with pytest.raises(ValueError, match="unregistered"):
        build_rail("xrocket", None)
