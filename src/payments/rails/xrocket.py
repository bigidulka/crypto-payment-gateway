"""Current xRocket Pay API; the deprecated Legacy API is intentionally separate.

Contract: https://pay.api.xrocket.exchange/api/docs-json (checked 2026-10-28).
Verification uses invoice payment facts, never the requested invoice amount.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from decimal import Decimal

from src.payments.rails.base import (
    Rail,
    RailBalance,
    RailCapabilities,
    RailError,
    RailInvoice,
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayment,
    RailPayout,
    RailPayoutRequest,
    RailType,
    WebhookRejected,
)
from src.payments.rails.http import ProviderHttp, amount, identifier, money_sum, payment_url

API_BASES = {
    "MAIN_NET": "https://pay.api.xrocket.exchange",
    "TEST_NET": "https://pay.api.testnet.xrocket.exchange",
}
# Signature-Timestamp freshness bound for Current API pushes.
WEBHOOK_WINDOW_MS = 300_000
STATUS = {
    "active": RailInvoiceStatus.PENDING,
    "partially_paid": RailInvoiceStatus.PENDING,
    "paid": RailInvoiceStatus.PAID,
    "expired": RailInvoiceStatus.EXPIRED,
    "cancelled": RailInvoiceStatus.FAILED,
}


class XRocketRail(Rail):
    rail_type = RailType.XROCKET

    def __init__(self, token: str, *, network: str = "MAIN_NET", base_url: str | None = None):
        if not token or network not in API_BASES:
            raise RailError("xrocket", "token and supported network are required", permanent=True)
        self._http = ProviderHttp(
            "xrocket", base_url or API_BASES[network], {"Authorization": f"Bearer {token}"}
        )

    @property
    def capabilities(self) -> RailCapabilities:
        return RailCapabilities("transfer", True, True, True)

    def supported_assets(self) -> tuple[str, ...]:
        return ("USDT", "USDC", "TON", "BTC", "ETH")

    async def account_identity(self) -> str:
        data = await self._http.request("GET", "/api/v1/app-info")
        return identifier(data.get("id"), "xrocket", "application id")

    def _invoice(self, data: dict) -> RailInvoice:
        invoice_id = identifier(data.get("id"), "xrocket", "invoice id")
        asset = data.get("priceCurrency")
        if asset not in self.supported_assets() or data.get("status") not in STATUS:
            raise RailError("xrocket", "invalid invoice asset or status", permanent=True)
        links = data.get("links")
        if not isinstance(links, dict):
            raise RailError("xrocket", "invoice has no payment links", permanent=True)
        return RailInvoice(
            invoice_id,
            STATUS[data["status"]],
            pay_url=payment_url(links.get("telegramBotLink"), "xrocket"),
            amount=amount(data.get("priceAmount"), "xrocket"),
            asset=asset,
            raw=data,
        )

    async def create_invoice(self, request: RailInvoiceRequest) -> RailInvoice:
        if request.asset not in self.supported_assets() or not request.external_id:
            raise RailError(
                "xrocket", "supported asset and client invoice id are required", permanent=True
            )
        requested = amount(request.amount, "xrocket")
        data = await self._http.request(
            "POST",
            "/api/v1/invoices",
            {
                "priceAmount": str(requested),
                "priceCurrency": request.asset,
                "payoutCurrency": request.asset,
                "payCurrencies": [request.asset],
                "numPayments": 1,
                "clientInvoiceId": request.external_id,
                "description": request.description,
                "expiresIn": request.expires_in_minutes * 60000,
            },
            mutation=True,
        )
        try:
            invoice = self._invoice(data)
            if (
                invoice.amount != requested
                or invoice.asset != request.asset
                or data.get("clientInvoiceId") != request.external_id
            ):
                raise RailError("xrocket", "created invoice binding mismatch")
        except RailError as exc:
            raise RailError(
                "xrocket", "created invoice cannot be safely bound", outcome_unknown=True
            ) from exc
        return invoice

    async def get_invoice(self, provider_invoice_id: str) -> RailInvoice:
        data = await self._http.request(
            "GET", "/api/v1/invoice", {"invoiceId": provider_invoice_id}
        )
        invoice = self._invoice(data)
        if invoice.provider_invoice_id != provider_invoice_id:
            raise RailError("xrocket", "invoice identity mismatch", permanent=True)
        return invoice

    async def find_invoice(self, external_id: str) -> RailInvoice | None:
        data = await self._http.request("GET", "/api/v1/invoice", {"clientInvoiceId": external_id})
        if data.get("clientInvoiceId") != external_id:
            raise RailError("xrocket", "client invoice identity mismatch", permanent=True)
        return self._invoice(data)

    async def verify_payment(self, provider_invoice_id: str) -> RailPayment | None:
        invoice = await self.get_invoice(provider_invoice_id)
        if invoice.status != RailInvoiceStatus.PAID:
            return None
        data = await self._http.request(
            "GET", "/api/v1/invoice/payments", {"invoiceId": provider_invoice_id, "limit": 1000}
        )
        items, pagination = data.get("items"), data.get("pagination")
        if (
            not isinstance(items, list)
            or not isinstance(pagination, dict)
            or pagination.get("next")
        ):
            raise RailError("xrocket", "incomplete payment evidence; reconciliation required")
        total = Decimal("0")
        settled = Decimal("0")
        seen: set[str] = set()
        for payment in items:
            if not isinstance(payment, dict):
                raise RailError("xrocket", "malformed invoice payment")
            if payment.get("status") in {"expired", "failed", "cancelled"}:
                # A previous terminal attempt is not a receipt. Nonzero money
                # on such an attempt still requires operator reconciliation.
                if (
                    amount(payment.get("payAmount"), "xrocket", positive=False) != 0
                    or amount(payment.get("receiveAmount"), "xrocket", positive=False) != 0
                ):
                    raise RailError("xrocket", "nonzero failed payment; reconciliation required")
                continue
            if payment.get("status") != "paid":
                raise RailError("xrocket", "non-final invoice payment; reconciliation required")
            payment_id = identifier(payment.get("id"), "xrocket", "payment id")
            if (
                payment_id in seen
                or payment.get("payCurrency") != invoice.asset
                or payment.get("receiveCurrency") != invoice.asset
            ):
                raise RailError(
                    "xrocket", "duplicate payment or settlement asset mismatch", permanent=True
                )
            seen.add(payment_id)
            total = money_sum("xrocket", total, amount(payment.get("payAmount"), "xrocket"))
            settled = money_sum("xrocket", settled, amount(payment.get("receiveAmount"), "xrocket"))
        if not seen:
            raise RailError("xrocket", "paid invoice lacks payment evidence")
        if settled > total:
            raise RailError(
                "xrocket", "unexpected settlement exceeds payment; reconciliation required"
            )
        return RailPayment(
            provider_invoice_id,
            total,
            invoice.asset,
            raw={"invoice": invoice.raw, "payments": items},
            settled_amount=settled,
        )

    async def get_balances(self) -> tuple[RailBalance, ...]:
        data = await self._http.request("GET", "/api/v1/balances")
        rows = data.get("balances")
        if not isinstance(rows, list):
            raise RailError("xrocket", "malformed balance response")
        result: list[RailBalance] = []
        seen: set[str] = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("asset") in seen
                or not isinstance(row.get("asset"), str)
            ):
                raise RailError("xrocket", "duplicate or malformed balance row")
            seen.add(row["asset"])
            available = amount(row.get("available"), "xrocket", positive=False)
            holds = amount(row.get("holds"), "xrocket", positive=False)
            if amount(row.get("balance"), "xrocket", positive=False) != money_sum(
                "xrocket", available, holds
            ):
                raise RailError("xrocket", "inconsistent provider balance")
            result.append(RailBalance(row["asset"], available, holds))
        return tuple(result)

    def _payout(self, data: dict, operation_id: str) -> RailPayout:
        if data.get("clientPayoutId") != operation_id or data.get("status") not in {
            "finished",
            "pending",
            "failed",
        }:
            raise RailError("xrocket", "payout binding or status mismatch", outcome_unknown=True)
        return RailPayout(
            data["status"] == "finished",
            provider_payout_id=identifier(data.get("payoutId"), "xrocket", "payout id"),
            status={"finished": "completed", "pending": "pending", "failed": "failed"}[
                data["status"]
            ],
            raw=data,
            amount=amount(data.get("amount"), "xrocket"),
            asset=identifier(data.get("asset"), "xrocket", "payout asset"),
            destination=identifier(data.get("target"), "xrocket", "payout target"),
            destination_type=identifier(data.get("targetType"), "xrocket", "payout target type"),
        )

    async def payout(self, request: RailPayoutRequest) -> RailPayout:
        if not request.operation_id or len(request.operation_id) > 50:
            raise RailError("xrocket", "durable payout operation id is required", permanent=True)
        if (
            not request.destination.isascii()
            or not request.destination.isdecimal()
            or int(request.destination) <= 0
        ):
            raise RailError("xrocket", "numeric Telegram recipient is required", permanent=True)
        requested = amount(request.amount, "xrocket")
        if request.asset not in self.supported_assets():
            raise RailError("xrocket", "unsupported payout asset", permanent=True)
        if request.destination_type not in {"telegram_user_id", "user_id"}:
            raise RailError("xrocket", "unsupported recipient type", permanent=True)
        data = await self._http.request(
            "POST",
            "/api/v1/payouts",
            {
                "clientPayoutId": request.operation_id,
                "target": request.destination,
                "targetType": request.destination_type,
                "asset": request.asset,
                "amount": str(requested),
            },
            mutation=True,
        )
        try:
            result = self._payout(data, request.operation_id)
            if (
                result.asset != request.asset
                or result.amount != requested
                or result.destination != request.destination
                or result.destination_type != request.destination_type
            ):
                raise RailError("xrocket", "payout request binding mismatch")
        except RailError as exc:
            raise RailError(
                "xrocket", "payout requires reconciliation", outcome_unknown=True
            ) from exc
        return result

    async def get_payout(self, operation_id: str, provider_payout_id: str = "") -> RailPayout:
        data = await self._http.request("GET", "/api/v1/payout", {"clientPayoutId": operation_id})
        result = self._payout(data, operation_id)
        if provider_payout_id and result.provider_payout_id != provider_payout_id:
            raise RailError("xrocket", "payout identity mismatch", outcome_unknown=True)
        return result

    def webhook_invoice_id(
        self, body: bytes, headers: Mapping[str, str], secret: str = ""
    ) -> str | None:
        # Current API: HMAC-SHA256("{Signature-Timestamp}.{body}") keyed by the
        # app webhook secret as-is; the API token is a different credential.
        timestamp = headers.get("signature-timestamp", "")
        if (
            not secret
            or headers.get("signature-version") != "v1"
            or not timestamp.isascii()
            or not timestamp.isdecimal()
            or abs(time.time() * 1000 - int(timestamp)) > WEBHOOK_WINDOW_MS
        ):
            raise WebhookRejected("xrocket signature headers are missing or stale")
        expected = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(headers.get("signature", ""), expected):
            raise WebhookRejected("xrocket signature mismatch")
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("type") != "invoice":
            return None
        event = data.get("data")
        invoice = event.get("invoice") if isinstance(event, dict) else None
        if not isinstance(invoice, dict):
            return None
        invoice_id = invoice.get("id")
        if not isinstance(invoice_id, (int, str)) or isinstance(invoice_id, bool):
            return None
        return str(invoice_id) or None

    async def close(self) -> None:
        await self._http.close()
