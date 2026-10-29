"""Server-driven Telegram checkout: merchant profile, inline screens and the bot credit feed.

A bot renders the screens it receives and credits only feed items. Session state
lives in Redis and is bound to the Telegram user who opened the menu; payment
facts come only from confirmed invoices with a gateway receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from html import escape
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Numeric, and_, case, cast, exists, false, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB, insert

from src.blockchain.chains import (
    ChainType,
    get_chain_config,
    get_evm_chains,
    is_chain_supported,
)
from src.core.config import get_settings
from src.core.exceptions import AppException, ValidationError
from src.db.models import (
    CheckoutPaymentAck,
    CheckoutProfile,
    DepositAddress,
    GatewayOperation,
    GatewayRailConnection,
    Invoice,
    InvoiceStatus,
    LedgerAsset,
    Merchant,
    OnchainTx,
    PaymentSession,
    PaymentSessionStatus,
    ProviderPaymentObservation,
    ProviderPaymentReceipt,
    ProviderSettlement,
    Rail,
    TxStatus,
)
from src.db.models.onchain_gateway import OnchainReceipt
from src.payments.policy import gateway_onchain_enabled
from src.payments.rails.base import RailError
from src.payments.telegram_texts import CHAIN_NAMES, LANGUAGES, text
from src.services.gateway_provider_service import GatewayError, GatewayProviderService
from src.services.invoice_service import InvoiceService

logger = logging.getLogger(__name__)

TOKEN = "USDT"
# Stablecoins settled 1:1 as the profile's USD.
ASSETS = ("USDT", "USDC")
CURRENCY = "USD"
CHAINS = ("ton", "solana")


def checkout_chains() -> tuple[str, ...]:
    return CHAINS + tuple(get_evm_chains())


CENT = Decimal("0.01")
CALLBACK_PREFIX = "ap:"
CALLBACK_MAX_BYTES = 64
INPUT_TTL_SECONDS = 5 * 60
POLL_THROTTLE_SECONDS = 5
# Outlives one invoice issuance: provider create (30 s timeout) plus database work.
LOCK_SECONDS = 120
LOCK_WAIT_SECONDS = 8.0
FEED_SCAN_BATCHES = 10
_CALLBACK = re.compile(r"^ap:([A-Za-z0-9_-]{1,16}|-):([a-z])([0-9a-z]{0,16})$")
_AMOUNT = re.compile(r"^[0-9]{1,7}(\.[0-9]{1,2})?$")
REQUESTED_PATTERN = r"^[0-9]{1,9}(\.[0-9]{1,2})?$"
USER_ID_PATTERN = r"^[1-9][0-9]{0,17}$"
_REQUESTED = re.compile(REQUESTED_PATTERN)
_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


def _exact(value: Any) -> Any:
    if isinstance(value, (float, bool)):
        raise ValueError("Use an exact decimal string or integer")
    return value


class CheckoutMethod(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=32)
    surcharge_percent: Decimal = Field(default=Decimal(0), ge=0, le=50, decimal_places=2)
    # Chain methods only; provider rails settle in USDT.
    token: Literal["USDT", "USDC"] = TOKEN

    @field_validator("surcharge_percent", mode="before")
    @classmethod
    def exact_surcharge(cls, value):
        return _exact(value)

    @field_validator("method")
    @classmethod
    def method_identity(cls, value):
        value = value.strip().lower()
        if value in checkout_chains():
            return value
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("method must be a provider rail id or a configured chain") from None

    @model_validator(mode="after")
    def rail_token(self):
        if not self.is_chain and self.token != TOKEN:
            raise ValueError("provider rail methods settle in USDT")
        return self

    @property
    def is_chain(self) -> bool:
        return self.method in checkout_chains()

    @property
    def short_id(self) -> str:
        """Stable button id: a profile edit never redirects an old button to another method."""
        if not self.is_chain:
            return UUID(self.method).hex[:8]
        return self.method if self.token == TOKEN else self.method + self.token.lower()


class CheckoutProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_title: str = Field(min_length=1, max_length=64)
    currency_label: Literal["USD"] = CURRENCY
    preset_amounts: list[Decimal] = Field(default_factory=list, max_length=9)
    min_amount: Decimal = Field(gt=0, le=100000, decimal_places=2)
    max_amount: Decimal = Field(gt=0, le=100000, decimal_places=2)
    custom_amount: bool = True
    methods: list[CheckoutMethod] = Field(min_length=1, max_length=8)
    default_language: Literal["en", "ru"] = "en"
    invoice_ttl_minutes: int = Field(default=60, ge=10, le=1440)
    # Pre-checkout invoices (metadata product + telegram_user_id) credited by the feed.
    legacy_products: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=8
    )

    @field_validator("min_amount", "max_amount", mode="before")
    @classmethod
    def exact_bounds(cls, value):
        return _exact(value)

    @field_validator("preset_amounts", mode="before")
    @classmethod
    def exact_presets(cls, value):
        if isinstance(value, list):
            for item in value:
                _exact(item)
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.min_amount > self.max_amount:
            raise ValueError("min_amount must not exceed max_amount")
        for amount in self.preset_amounts:
            if amount != amount.quantize(CENT) or not self.min_amount <= amount <= self.max_amount:
                raise ValueError(
                    "preset amounts must have at most 2 decimals and lie within min..max"
                )
        if len(set(self.preset_amounts)) != len(self.preset_amounts):
            raise ValueError("preset amounts must be unique")
        if not self.preset_amounts and not self.custom_amount:
            raise ValueError("configure preset amounts or allow a custom amount")
        if len({m.short_id for m in self.methods}) != len(self.methods):
            raise ValueError("payment methods must be unique")
        return self


def surcharged(amount: Decimal, percent: Decimal) -> Decimal:
    """What the customer pays: the merchant never receives less than the requested amount."""
    return (amount * (100 + percent) / 100).quantize(CENT, rounding=ROUND_UP)


def usd(value: Decimal) -> str:
    value = value.quantize(CENT)
    return f"${value:.0f}" if value == value.to_integral_value() else f"${value:.2f}"


def token_amount(value: Decimal) -> str:
    return f"{value:.2f}" if value == value.quantize(CENT) else format(value.normalize(), "f")


@dataclass
class Screen:
    text: str | None
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    expect_input: bool = False
    toast: str | None = None
    paid: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "buttons": self.buttons,
            "expect_input": self.expect_input,
            "toast": self.toast,
            "paid": self.paid,
        }


def callback(sid: str, op: str) -> str:
    data = f"{CALLBACK_PREFIX}{sid}:{op}"
    if len(data.encode()) > CALLBACK_MAX_BYTES:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return data


class TelegramCheckoutService:
    def __init__(self, provider: GatewayProviderService, redis):
        self.provider = provider
        self.factory = provider.factory
        self.settings = provider.settings
        self.redis = redis

    # --- profile ---

    async def get_profile(self, merchant_id: UUID) -> CheckoutProfileConfig | None:
        async with self.factory() as session:
            row = await session.get(CheckoutProfile, merchant_id)
        return CheckoutProfileConfig.model_validate(row.profile) if row else None

    async def put_profile(
        self, merchant_id: UUID, profile: CheckoutProfileConfig
    ) -> CheckoutProfileConfig:
        async with self.factory() as session, session.begin():
            for method in profile.methods:
                if method.is_chain:
                    if not self._chain_selectable(merchant_id, method.method, method.token):
                        raise ValidationError(
                            f"On-chain method {method.method} {method.token} is not enabled"
                        )
                    continue
                self.provider.require_enabled(merchant_id)
                rail = await session.scalar(
                    select(Rail)
                    .join(GatewayRailConnection, GatewayRailConnection.rail_id == Rail.id)
                    .where(
                        Rail.id == UUID(method.method),
                        Rail.merchant_id == merchant_id,
                        Rail.is_active.is_(True),
                    )
                )
                if rail is None or TOKEN not in (rail.assets or []):
                    raise ValidationError("Provider method is unavailable for USDT")
            stored = profile.model_dump(mode="json")
            await session.execute(
                insert(CheckoutProfile)
                .values(merchant_id=merchant_id, profile=stored)
                .on_conflict_do_update(
                    index_elements=[CheckoutProfile.merchant_id],
                    set_={"profile": stored, "updated_at": func.now()},
                )
            )
        return profile

    def _chain_selectable(self, merchant_id: UUID, chain: str, token: str) -> bool:
        """Mirror of PaymentService.select_payment_option gating for this merchant."""
        if not is_chain_supported(chain):
            return False
        config = get_chain_config(chain)
        if not config.supports_asset(token) or config.is_native_asset(token):
            return False
        modern = gateway_onchain_enabled(merchant_id, self.settings, chain)
        if config.chain_type != ChainType.EVM:
            return self.settings.non_evm_enabled and modern
        if not modern:
            # EVM outside the receipt rollout stays on the poller path for every merchant.
            return True
        try:
            blocks = json.loads(self.settings.non_evm_start_blocks)
        except ValueError:
            return False
        start = blocks.get(chain) if isinstance(blocks, dict) else None
        return isinstance(start, int) and not isinstance(start, bool) and start >= 1

    # --- screens ---

    def _key(self, merchant_id: UUID, *parts: object) -> str:
        return ":".join(["checkout", "tg", str(merchant_id), *(str(p) for p in parts)])

    async def _load(self, merchant_id, sid) -> dict | None:
        raw = await self.redis.get(self._key(merchant_id, "s", sid))
        return json.loads(raw) if raw else None

    async def _save(self, merchant_id, sid, state, profile) -> None:
        ttl = profile.invoice_ttl_minutes * 60 + 3600
        await self.redis.set(self._key(merchant_id, "s", sid), json.dumps(state), ex=ttl)

    async def handle(
        self, merchant_id: UUID, user_id: int, lang: str | None, action: dict
    ) -> Screen:
        profile = await self.get_profile(merchant_id)
        if profile is None:
            raise GatewayError("Checkout profile is not configured", status_code=404)
        lang = lang if lang in LANGUAGES else profile.default_language
        kind = action.get("type")
        if kind == "start":
            return await self._start(merchant_id, user_id, lang, profile)
        if kind == "input":
            sid = await self.redis.get(self._key(merchant_id, "input", user_id))
            if not sid:
                return self._expired(lang)
            async with self._session_lock(merchant_id, sid) as owned:
                if not owned:
                    return Screen(None, toast=text(lang, "wait"))
                state = await self._load(merchant_id, sid)
                if state is None or state["uid"] != user_id or state["step"] != "custom":
                    return self._expired(lang)
                return await self._input(merchant_id, sid, state, lang, profile, action["text"])
        match = _CALLBACK.match(action.get("data") or "")
        if kind != "callback" or match is None:
            return Screen(None, toast=text(lang, "unknown"))
        sid, op, arg = match.groups()
        if sid == "-":
            if op == "n":
                return await self._start(merchant_id, user_id, lang, profile)
            return self._expired(lang)
        # Every session write happens under the owner lock, so no press can
        # overwrite an invoice another press of the same menu just issued.
        async with self._session_lock(merchant_id, sid) as owned:
            state = await self._load(merchant_id, sid)
            if state is not None and state["uid"] != user_id:
                return Screen(None, toast=text(lang, "foreign"))
            if not owned:
                return Screen(None, toast=text(lang, "wait"))
            if op == "n":
                return await self._start(merchant_id, user_id, lang, profile)
            if state is None:
                return self._expired(lang)
            return await self._press(merchant_id, sid, state, lang, profile, op, arg)

    @asynccontextmanager
    async def _session_lock(self, merchant_id, sid):
        key = self._key(merchant_id, "lock", sid)
        token = secrets.token_hex(16)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCK_WAIT_SECONDS
        owned = bool(await self.redis.set(key, token, nx=True, ex=LOCK_SECONDS))
        while not owned and loop.time() < deadline:
            await asyncio.sleep(0.2)
            owned = bool(await self.redis.set(key, token, nx=True, ex=LOCK_SECONDS))
        try:
            yield owned
        finally:
            if owned:
                # Compare-and-delete: an expired lock taken over by another press stays its own.
                await self.redis.eval(_RELEASE, 1, key, token)

    def _expired(self, lang) -> Screen:
        return Screen(text(lang, "session_expired"), [[self._button(lang, "new", "-", "n")]])

    @staticmethod
    def _button(lang, key, sid, op) -> dict[str, str]:
        return {"text": text(lang, key), "callback_data": callback(sid, op)}

    async def _start(self, merchant_id, user_id, lang, profile) -> Screen:
        sid = secrets.token_urlsafe(8)
        state = {"uid": user_id, "step": "amount", "amount": None, "invoice": None, "attempt": 0}
        await self._save(merchant_id, sid, state, profile)
        return self._amount_screen(sid, lang, profile)

    async def _press(self, merchant_id, sid, state, lang, profile, op, arg) -> Screen:
        if op == "x":
            await self.redis.delete(self._key(merchant_id, "input", state["uid"]))
            state.update(step="closed", invoice=None)
            await self._save(merchant_id, sid, state, profile)
            return Screen(text(lang, "cancelled"), [[self._button(lang, "new", sid, "n")]])
        if state["step"] == "closed":
            return self._expired(lang)
        if state["invoice"] and op in {"a", "c", "m", "k"}:
            return await self._invoice_screen(
                merchant_id, sid, state, lang, profile, check=op == "k"
            )
        if op == "a":
            amount = next(
                (a for a in profile.preset_amounts if arg.isdigit() and a * 100 == int(arg)), None
            )
            if amount is None:
                screen = self._amount_screen(sid, lang, profile)
                screen.toast = text(lang, "unavailable")
                return screen
            state.update(step="method", amount=str(amount.quantize(CENT)))
            await self._save(merchant_id, sid, state, profile)
            return self._method_screen(sid, state, lang, profile)
        if op == "c" and profile.custom_amount:
            state.update(step="custom", amount=None)
            await self._save(merchant_id, sid, state, profile)
            await self.redis.set(
                self._key(merchant_id, "input", state["uid"]), sid, ex=INPUT_TTL_SECONDS
            )
            return self._custom_screen(sid, lang, profile)
        if op == "m" and state["amount"]:
            method = next((m for m in profile.methods if m.short_id == arg), None)
            if method is None:
                screen = self._method_screen(sid, state, lang, profile)
                screen.toast = text(lang, "unavailable")
                return screen
            return await self._choose(merchant_id, sid, state, lang, profile, method)
        if op == "b":
            await self.redis.delete(self._key(merchant_id, "input", state["uid"]))
            if state["step"] == "invoice" and state["amount"]:
                # The left invoice stays payable and credited; a new choice issues a new one.
                state.update(step="method", invoice=None, attempt=state["attempt"] + 1)
                await self._save(merchant_id, sid, state, profile)
                return self._method_screen(sid, state, lang, profile)
            state.update(step="amount", amount=None, invoice=None)
            await self._save(merchant_id, sid, state, profile)
            return self._amount_screen(sid, lang, profile)
        return await self._current(merchant_id, sid, state, lang, profile)

    async def _current(self, merchant_id, sid, state, lang, profile) -> Screen:
        if state["step"] == "method" and state["amount"]:
            return self._method_screen(sid, state, lang, profile)
        if state["step"] == "custom":
            return self._custom_screen(sid, lang, profile)
        return self._amount_screen(sid, lang, profile)

    async def _input(self, merchant_id, sid, state, lang, profile, raw: str) -> Screen:
        value = (raw or "").strip().replace(",", ".").removeprefix("$").strip()
        bounds = {"min": usd(profile.min_amount), "max": usd(profile.max_amount)}
        if not _AMOUNT.match(value):
            return self._custom_screen(
                sid, lang, profile, error=text(lang, "custom_invalid", example="7.50")
            )
        amount = Decimal(value).quantize(CENT)
        if not profile.min_amount <= amount <= profile.max_amount:
            return self._custom_screen(
                sid, lang, profile, error=text(lang, "custom_range", **bounds)
            )
        await self.redis.delete(self._key(merchant_id, "input", state["uid"]))
        state.update(step="method", amount=str(amount))
        await self._save(merchant_id, sid, state, profile)
        return self._method_screen(sid, state, lang, profile)

    def _title(self, profile) -> str:
        return f"<b>{escape(profile.product_title, quote=False)}</b>"

    def _amount_screen(self, sid, lang, profile) -> Screen:
        presets = [
            {"text": usd(amount), "callback_data": callback(sid, f"a{int(amount * 100)}")}
            for amount in profile.preset_amounts
        ]
        rows = [presets[i : i + 3] for i in range(0, len(presets), 3)]
        if profile.custom_amount:
            rows.append([self._button(lang, "custom", sid, "c")])
        rows.append([self._button(lang, "cancel", sid, "x")])
        return Screen(f"{self._title(profile)}\n\n{text(lang, 'choose_amount')}", rows)

    def _custom_screen(self, sid, lang, profile, *, error: str | None = None) -> Screen:
        prompt = text(
            lang,
            "custom_prompt",
            currency=CURRENCY,
            min=usd(profile.min_amount),
            max=usd(profile.max_amount),
            example="7.50",
        )
        body = f"{self._title(profile)}\n\n{prompt}"
        if error:
            body += f"\n\n<i>{error}</i>"
        rows = [[self._button(lang, "back", sid, "b"), self._button(lang, "cancel", sid, "x")]]
        return Screen(body, rows, expect_input=True, toast=error)

    def _method_screen(self, sid, state, lang, profile, *, note: str | None = None) -> Screen:
        amount = Decimal(state["amount"])
        rows = []
        for method in profile.methods:
            label = method.label
            if method.surcharge_percent:
                label += f" · +{method.surcharge_percent.normalize():f}% {text(lang, 'fee')}"
            rows.append([{"text": label, "callback_data": callback(sid, f"m{method.short_id}")}])
        rows.append([self._button(lang, "back", sid, "b"), self._button(lang, "cancel", sid, "x")])
        body = (
            f"{self._title(profile)}\n{text(lang, 'amount')}: <b>{usd(amount)}</b>\n\n"
            f"{text(lang, 'choose_method')}"
        )
        if note:
            body += f"\n\n<i>{note}</i>"
        return Screen(body, rows, toast=note)

    async def _choose(self, merchant_id, sid, state, lang, profile, method) -> Screen:
        """Runs under the session lock; the invoice id is stored before it is released."""
        try:
            invoice_id = await self._issue(merchant_id, sid, state, profile, method)
        except (AppException, RailError) as exc:
            logger.warning("checkout invoice was not issued: %s", type(exc).__name__)
            state["attempt"] += 1
            await self._save(merchant_id, sid, state, profile)
            return self._method_screen(sid, state, lang, profile, note=text(lang, "failed"))
        state.update(step="invoice", invoice=str(invoice_id))
        await self._save(merchant_id, sid, state, profile)
        return await self._invoice_screen(merchant_id, sid, state, lang, profile)

    async def _issue(self, merchant_id, sid, state, profile, method: CheckoutMethod) -> UUID:
        amount = Decimal(state["amount"])
        identity = f"{merchant_id}:{sid}:{method.method}:{amount}:{state['attempt']}"
        key = "tgco:" + hashlib.sha256(identity.encode()).hexdigest()[:48]
        metadata = {
            "checkout": True,
            "telegram_user_id": state["uid"],
            "requested_amount": str(amount),
            "surcharge_percent": format(method.surcharge_percent.normalize(), "f"),
        }
        async with self.factory() as session:
            merchant = await session.get(Merchant, merchant_id)
            invoice = await InvoiceService(session).create_invoice(
                merchant,
                surcharged(amount, method.surcharge_percent),
                method.token,
                [method.method] if method.is_chain else [],
                ttl_minutes=profile.invoice_ttl_minutes,
                metadata=metadata,
                idempotency_key=key,
                allowed_rails=None if method.is_chain else [UUID(method.method)],
                customer_telegram_id=str(state["uid"]),
            )
            invoice_id, public_id = invoice.id, invoice.public_id
        if method.is_chain:
            await self._select_onchain(merchant_id, invoice_id, method.method, method.token)
        else:
            operation = await self.provider.select_invoice(public_id, UUID(method.method))
            if operation.status != "pending" or not operation.pay_url:
                raise GatewayError("Provider invoice is not payable")
        return invoice_id

    async def _select_onchain(
        self, merchant_id: UUID, invoice_id: UUID, chain: str, token: str
    ) -> None:
        from src.services.payment_service import PaymentService

        async with self.factory() as session:
            invoice = await InvoiceService(session).get_invoice(invoice_id, merchant_id)
            await PaymentService(session).select_payment_option(invoice, chain, token)

    async def _invoice_screen(
        self, merchant_id, sid, state, lang, profile, *, check: bool = False
    ) -> Screen:
        invoice_id = UUID(state["invoice"])
        async with self.factory() as session:
            operation = await session.scalar(
                select(GatewayOperation).where(
                    GatewayOperation.invoice_id == invoice_id,
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.operation_type == "invoice",
                )
            )
        if (
            check
            and operation is not None
            and operation.status in {"pending", "unknown", "submitting"}
        ):
            throttle = self._key(merchant_id, "poll", invoice_id)
            if await self.redis.set(throttle, "1", nx=True, ex=POLL_THROTTLE_SECONDS):
                try:
                    await self.provider.poll_invoice(merchant_id, operation.id)
                except (AppException, RailError) as exc:
                    logger.warning("checkout poll failed: %s", type(exc).__name__)
        async with self.factory() as session:
            invoice = await session.scalar(
                select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
            )
            operation = await session.scalar(
                select(GatewayOperation).where(
                    GatewayOperation.invoice_id == invoice_id,
                    GatewayOperation.operation_type == "invoice",
                )
            )
            payment = (
                await session.execute(
                    select(PaymentSession, DepositAddress)
                    .join(DepositAddress, DepositAddress.id == PaymentSession.deposit_address_id)
                    .where(PaymentSession.invoice_id == invoice_id)
                )
            ).first()
        if invoice is None:
            return self._expired(lang)
        reference = escape(invoice.public_id, quote=False)
        header = f"{self._title(profile)}\n{text(lang, 'invoice')} <code>{reference}</code>\n"
        new = [[self._button(lang, "new", sid, "n")]]
        if invoice.status == InvoiceStatus.CONFIRMED:
            item = await self.payment(merchant_id, invoice_id, invoice=invoice)
            if item is None:
                return Screen(header + "\n" + text(lang, "review"), new)
            credited = usd(Decimal(item["credit_amount"]))
            return Screen(header + "\n✅ " + text(lang, "paid", amount=credited), new, paid=True)
        if operation is not None and operation.status == "reconciliation_required":
            return Screen(header + "\n" + text(lang, "review"), new)
        # A transfer seen before expiry may still confirm; only an untouched address is final.
        expired = (
            invoice.status == InvoiceStatus.EXPIRED
            or (operation is not None and operation.status == "failed")
            or (
                operation is None
                and invoice.is_expired
                and (payment is None or not payment[0].received_amount)
            )
        )
        if expired:
            return Screen(header + "\n" + text(lang, "expired"), new)
        requested = Decimal((invoice.extra_data or {}).get("requested_amount", invoice.amount))
        lines = [header + f"{text(lang, 'amount')}: <b>{usd(requested)}</b>"]
        controls = [
            [self._button(lang, "check", sid, "k")],
            [self._button(lang, "back", sid, "b")],
        ]
        surcharge = Decimal(str((invoice.extra_data or {}).get("surcharge_percent", "0")))
        if operation is not None and operation.pay_url:
            method = self._method_label(profile, str(operation.rail_id))
            due = f"{token_amount(invoice.amount)} {invoice.asset}"
            to_pay = f"{text(lang, 'to_pay')}: <b>{due}</b>"
            if surcharge:
                to_pay += f" (+{surcharge.normalize():f}% {text(lang, 'fee')})"
            lines += [to_pay, "", text(lang, "custodial_hint")]
            controls.insert(
                0, [{"text": text(lang, "pay_via", method=method), "url": operation.pay_url}]
            )
        elif payment is not None:
            session_row, address = payment
            network = CHAIN_NAMES.get(session_row.chain) or get_chain_config(session_row.chain).name
            expires = invoice.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            lines += [
                f"{text(lang, 'send')}: <code>{token_amount(invoice.amount)}</code> "
                + invoice.asset,
                f"{text(lang, 'network')}: {escape(network, quote=False)}",
                text(lang, "address"),
                f"<code>{escape(address.address, quote=False)}</code>",
                f"{text(lang, 'pay_before')}: {expires}",
                "",
                "<i>"
                + text(
                    lang, "onchain_note", token=invoice.asset, network=escape(network, quote=False)
                )
                + "</i>",
                text(lang, "onchain_auto"),
            ]
            received = session_row.received_amount
            if session_row.mismatch_reason == "underpaid" and received is not None:
                lines += [
                    "",
                    "<b>"
                    + text(
                        lang,
                        "underpaid",
                        received=token_amount(received),
                        amount=token_amount(invoice.amount),
                        missing=token_amount(invoice.amount - received),
                        token=invoice.asset,
                    )
                    + "</b>",
                ]
            elif session_row.mismatch_reason == "wrong_token":
                lines += [
                    "",
                    "<b>"
                    + text(
                        lang,
                        "wrong_token",
                        got=escape(session_row.mismatch_token or "?", quote=False),
                        token=invoice.asset,
                    )
                    + "</b>",
                ]
        else:
            return self._method_screen(
                sid, {**state, "amount": str(requested)}, lang, profile, note=text(lang, "failed")
            )
        toast = None
        if check:
            toast = text(lang, "not_received_toast")
            lines += ["", "⏳ " + text(lang, "not_received")]
        return Screen("\n".join(lines), controls, toast=toast)

    @staticmethod
    def _method_label(profile, method_id: str) -> str:
        for method in profile.methods:
            if method.method == method_id:
                return method.label
        return "provider"

    # --- credit feed ---

    def _eligible(self, merchant_id: UUID, legacy_products: list[str]):
        """SQL twin of the checks in payment(): an unpayable row must never enter the feed."""
        meta = cast(Invoice.extra_data, JSONB)
        user = meta["telegram_user_id"]
        requested = meta["requested_amount"]
        requested_value = case(
            (
                and_(
                    func.jsonb_typeof(requested) == "string",
                    requested.astext.op("~")(REQUESTED_PATTERN),
                ),
                cast(requested.astext, Numeric),
            ),
            else_=None,
        )
        is_checkout = func.coalesce(
            and_(
                func.jsonb_typeof(meta["checkout"]) == "boolean",
                meta["checkout"].astext == "true",
            ),
            False,
        )
        legacy = (
            and_(
                is_checkout.is_(False),
                func.jsonb_typeof(meta["product"]) == "string",
                meta["product"].astext.in_(legacy_products),
                Invoice.amount >= CENT,
            )
            if legacy_products
            else false()
        )
        receipts = [
            exists(
                select(ProviderPaymentReceipt.id)
                .join(
                    ProviderPaymentObservation,
                    ProviderPaymentObservation.id == ProviderPaymentReceipt.observation_id,
                )
                .join(
                    ProviderSettlement,
                    ProviderSettlement.observation_id == ProviderPaymentObservation.id,
                )
                .join(LedgerAsset, LedgerAsset.id == ProviderPaymentObservation.ledger_asset_id)
                .where(
                    ProviderPaymentReceipt.invoice_id == Invoice.id,
                    ProviderPaymentReceipt.merchant_id == merchant_id,
                    LedgerAsset.symbol == Invoice.asset,
                )
            )
        ]
        # The receipt endpoint shows chain receipts only while the chain gateway is on.
        if get_settings().onchain_gateway_enabled:
            receipts.append(
                exists(
                    select(OnchainReceipt.id)
                    .join(LedgerAsset, LedgerAsset.id == OnchainReceipt.asset_id)
                    .where(
                        OnchainReceipt.invoice_id == Invoice.id,
                        OnchainReceipt.merchant_id == merchant_id,
                        LedgerAsset.symbol == Invoice.asset,
                    )
                )
            )
        receipts.append(self._evm_evidence_sql())
        return and_(
            Invoice.merchant_id == merchant_id,
            Invoice.status == InvoiceStatus.CONFIRMED,
            Invoice.asset.in_(ASSETS),
            func.jsonb_typeof(user) == "number",
            user.astext.op("~")(USER_ID_PATTERN),
            or_(
                and_(is_checkout, requested_value >= CENT, requested_value <= Invoice.amount),
                legacy,
            ),
            or_(*receipts),
        )

    @staticmethod
    def _evm_assets() -> list[tuple[str, str, str]]:
        """(chain, token, lower-case contract) the EVM poller credits for checkout assets."""
        result = []
        for chain in get_evm_chains():
            config = get_chain_config(chain)
            for token in ASSETS:
                asset = config.get_token(token)
                if asset is not None:
                    result.append((chain, token, asset.contract_address.lower()))
        return result

    def _evm_evidence_sql(self):
        """SQL twin of _evm_evidence(): the legacy EVM poller path, which writes no receipt."""
        confirmed_sum = (
            select(func.sum(OnchainTx.amount))
            .where(
                OnchainTx.payment_session_id == PaymentSession.id,
                OnchainTx.status == TxStatus.CONFIRMED,
            )
            .scalar_subquery()
        )
        sessions = (
            select(func.count(PaymentSession.id))
            .where(PaymentSession.invoice_id == Invoice.id)
            .scalar_subquery()
        )
        assets = [
            and_(
                PaymentSession.chain == chain,
                PaymentSession.token == token,
                ~exists().where(
                    OnchainTx.payment_session_id == PaymentSession.id,
                    OnchainTx.token_contract != contract,
                ),
            )
            for chain, token, contract in self._evm_assets()
        ]
        return and_(
            ~exists().where(ProviderPaymentReceipt.invoice_id == Invoice.id),
            ~exists().where(OnchainReceipt.invoice_id == Invoice.id),
            sessions == 1,
            exists(
                select(PaymentSession.id).where(
                    PaymentSession.invoice_id == Invoice.id,
                    PaymentSession.status == PaymentSessionStatus.PAID,
                    PaymentSession.token == Invoice.asset,
                    or_(*assets) if assets else false(),
                    PaymentSession.received_amount >= Invoice.amount,
                    PaymentSession.received_amount == confirmed_sum,
                )
            ),
        )

    async def _evm_evidence(self, invoice: Invoice) -> tuple[str, Decimal] | None:
        """(chain, received) when the EVM poller confirmed exactly this invoice in full."""
        async with self.factory() as session:
            if await session.scalar(
                select(
                    exists().where(ProviderPaymentReceipt.invoice_id == invoice.id)
                    | exists().where(OnchainReceipt.invoice_id == invoice.id)
                )
            ):
                return None
            payments = (
                await session.scalars(
                    select(PaymentSession).where(PaymentSession.invoice_id == invoice.id)
                )
            ).all()
            if len(payments) != 1:
                return None
            payment = payments[0]
            txs = (
                await session.scalars(
                    select(OnchainTx).where(OnchainTx.payment_session_id == payment.id)
                )
            ).all()
        contract = {(c, t): k for c, t, k in self._evm_assets()}.get((payment.chain, payment.token))
        received = payment.received_amount
        confirmed = [tx.amount for tx in txs if tx.status == TxStatus.CONFIRMED]
        if (
            contract is None
            or payment.status != PaymentSessionStatus.PAID
            or payment.token != invoice.asset
            or any(tx.token_contract != contract for tx in txs)
            or received is None
            or received < invoice.amount
            or not confirmed
            or sum(confirmed, Decimal(0)) != received
        ):
            return None
        return payment.chain, received

    async def _legacy_products(self, merchant_id: UUID) -> list[str]:
        profile = await self.get_profile(merchant_id)
        return list(profile.legacy_products) if profile else []

    async def payments(
        self, merchant_id: UUID, limit: int, after: UUID | None = None
    ) -> list[dict]:
        """Oldest un-acknowledged payable items; `after` continues past a seen invoice."""
        self.provider.require_enabled(merchant_id)
        legacy = await self._legacy_products(merchant_id)
        cursor = None
        if after is not None:
            async with self.factory() as session:
                row = (
                    await session.execute(
                        select(Invoice.updated_at, Invoice.id).where(
                            Invoice.id == after, Invoice.merchant_id == merchant_id
                        )
                    )
                ).first()
            if row is None:
                raise ValidationError("Unknown feed cursor")
            cursor = tuple(row)
        items: list[dict] = []
        # Defence in depth: a row payment() still rejects is skipped, never blocks newer ones.
        for _ in range(FEED_SCAN_BATCHES):
            statement = select(Invoice).where(
                self._eligible(merchant_id, legacy),
                ~exists().where(CheckoutPaymentAck.invoice_id == Invoice.id),
            )
            if cursor is not None:
                statement = statement.where(tuple_(Invoice.updated_at, Invoice.id) > cursor)
            async with self.factory() as session:
                batch = (
                    await session.scalars(
                        statement.order_by(Invoice.updated_at, Invoice.id).limit(limit)
                    )
                ).all()
            for invoice in batch:
                cursor = (invoice.updated_at, invoice.id)
                item = await self.payment(
                    merchant_id, invoice.id, invoice=invoice, legacy_products=legacy
                )
                if item is not None:
                    items.append(item)
                    if len(items) == limit:
                        return items
            if len(batch) < limit:
                break
        return items

    async def payment(
        self,
        merchant_id: UUID,
        invoice_id: UUID,
        *,
        invoice: Invoice | None = None,
        legacy_products: list[str] | None = None,
    ) -> dict | None:
        """Credit instruction for a confirmed checkout invoice; None is never credited."""
        if legacy_products is None:
            legacy_products = await self._legacy_products(merchant_id)
        async with self.factory() as session:
            if invoice is None:
                invoice = await session.scalar(
                    select(Invoice).where(
                        Invoice.id == invoice_id, self._eligible(merchant_id, legacy_products)
                    )
                )
            acked = await session.scalar(
                select(exists().where(CheckoutPaymentAck.invoice_id == invoice_id))
            )
        if (
            invoice is None
            or invoice.status != InvoiceStatus.CONFIRMED
            or invoice.asset not in ASSETS
        ):
            return None
        meta = invoice.extra_data or {}
        user_id = meta.get("telegram_user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or not 0 < user_id < 10**18:
            return None
        if meta.get("checkout") is True:
            raw = meta.get("requested_amount")
            if not isinstance(raw, str) or not _REQUESTED.match(raw):
                return None
            requested = Decimal(raw)
        elif meta.get("product") in legacy_products and isinstance(meta.get("product"), str):
            requested = invoice.amount
        else:
            return None
        # The customer pays the requested amount plus the surcharge, never less.
        if requested < CENT or requested > invoice.amount:
            return None
        receipt = (await self.provider.receipt(merchant_id, invoice_id))["receipt"]
        if receipt is None:
            evidence = await self._evm_evidence(invoice)
            if evidence is None:
                return None
            network, gross = evidence
        elif receipt["asset"] != invoice.asset:
            return None
        else:
            gross = Decimal(receipt["gross"])
            network = receipt["network"]
        # Provider receipts carry "<rail type>:<network>"; chain evidence the chain id.
        custodial = ":" in network
        if custodial:
            credit = requested.quantize(CENT, rounding=ROUND_DOWN)
        else:
            # Anything received above the invoice belongs to the customer.
            credit = (requested + gross - invoice.amount).quantize(CENT, rounding=ROUND_DOWN)
        if credit <= 0:
            return None
        return {
            "invoice_id": str(invoice.id),
            "telegram_user_id": user_id,
            "credit_amount": str(credit),
            "currency": CURRENCY,
            "external_id": f"gateway-{invoice.id}",
            "method": network.split(":", 1)[0],
            "network": network,
            "gross": str(gross),
            "confirmed_at": invoice.updated_at.astimezone(UTC).isoformat(),
            "acked": bool(acked),
        }

    async def ack(self, merchant_id: UUID, invoice_id: UUID) -> dict | None:
        if await self.payment(merchant_id, invoice_id) is None:
            return None
        async with self.factory() as session, session.begin():
            await session.execute(
                insert(CheckoutPaymentAck)
                .values(invoice_id=invoice_id, merchant_id=merchant_id)
                .on_conflict_do_nothing(index_elements=[CheckoutPaymentAck.invoice_id])
            )
        return {"invoice_id": str(invoice_id), "acked": True}
