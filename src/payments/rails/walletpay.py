"""Wallet Pay / Walt Pay Store API 1.2.0 (checked 2026-10-08).

https://docs.walt.io/pay/integration-schema.yaml
Order preview is not settlement evidence; verification reads reconciliation.
There is no documented spendable-balance or payout API. Settlement is manual.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from src.payments.rails.base import (
    Rail,
    RailCapabilities,
    RailError,
    RailInvoice,
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayment,
    RailType,
)
from src.payments.rails.http import ProviderHttp, amount, identifier, money_sum, payment_url

API_BASE = "https://pay.walt.io"
API_PREFIX = "/wpay/store-api/v1"
STATUS = {
    "ACTIVE": RailInvoiceStatus.PENDING,
    "PAID": RailInvoiceStatus.PAID,
    "EXPIRED": RailInvoiceStatus.EXPIRED,
    "CANCELLED": RailInvoiceStatus.FAILED,
}


class WalletPayRail(Rail):
    rail_type = RailType.TGWALLET

    def __init__(self, token: str, *, network: str = "MAIN_NET", base_url: str | None = None):
        if not token or network != "MAIN_NET":
            raise RailError("tgwallet", "store API key and MAIN_NET are required", permanent=True)
        # The Store API has no getMe endpoint. Keep token identity explicit;
        # rotations require operator reconciliation and cannot rebind old facts.
        self._identity = "credential:" + hashlib.sha256(token.encode()).hexdigest()
        self._http = ProviderHttp("tgwallet", base_url or API_BASE, {"Wpay-Store-Api-Key": token})

    @property
    def capabilities(self) -> RailCapabilities:
        return RailCapabilities(
            payout_mode="manual", requires_telegram_customer=True, settlement_hold_seconds=48 * 3600
        )

    def supported_assets(self) -> tuple[str, ...]:
        return ("USDT", "TON", "BTC")

    async def account_identity(self) -> str:
        # Authentication is checked by a read-only reconciliation request.
        await self._api("GET", "/reconciliation/order-list", {"offset": 0, "count": 1})
        return self._identity

    async def _api(
        self, method: str, path: str, data: dict | None = None, *, mutation: bool = False
    ) -> dict:
        envelope = await self._http.request(method, API_PREFIX + path, data, mutation=mutation)
        status = envelope.get("status")
        if status not in {"SUCCESS", "ALREADY"}:
            definite = status in {"CONFLICT", "ACCESS_DENIED", "INVALID_REQUEST"}
            raise RailError(
                "tgwallet",
                "provider rejected request" if definite else "unknown provider result",
                permanent=definite,
                outcome_unknown=mutation and not definite,
            )
        result = envelope.get("data")
        if not isinstance(result, dict):
            raise RailError(
                "tgwallet", "provider returned malformed result", outcome_unknown=mutation
            )
        return result

    def _money(self, value: object, *, positive: bool = True) -> tuple[str, Decimal]:
        if not isinstance(value, dict) or value.get("currencyCode") not in self.supported_assets():
            raise RailError("tgwallet", "invalid settlement currency", permanent=True)
        return value["currencyCode"], amount(value.get("amount"), "tgwallet", positive=positive)

    def _invoice(self, data: dict) -> RailInvoice:
        invoice_id = identifier(data.get("id"), "tgwallet", "order id")
        if data.get("status") not in STATUS:
            raise RailError("tgwallet", "invalid order status", permanent=True)
        asset, requested = self._money(data.get("amount"))
        return RailInvoice(
            invoice_id,
            STATUS[data["status"]],
            pay_url=payment_url(data.get("directPayLink"), "tgwallet"),
            asset=asset,
            amount=requested,
            raw=data,
        )

    async def create_invoice(self, request: RailInvoiceRequest) -> RailInvoice:
        customer = request.customer_telegram_id
        if not customer.isascii() or not customer.isdecimal() or int(customer) <= 0:
            raise RailError("tgwallet", "verified Telegram customer id is required", permanent=True)
        if not request.external_id or request.asset not in self.supported_assets():
            raise RailError(
                "tgwallet", "supported asset and external order id are required", permanent=True
            )
        requested = amount(request.amount, "tgwallet")
        data = await self._api(
            "POST",
            "/order",
            {
                "amount": {"currencyCode": request.asset, "amount": str(requested)},
                "description": request.description or "Arbitron payment",
                "externalId": request.external_id,
                "timeoutSeconds": request.expires_in_minutes * 60,
                "customerTelegramUserId": int(customer),
                "autoConversionCurrency": request.asset,
            },
            mutation=True,
        )
        try:
            invoice = self._invoice(data)
            if invoice.asset != request.asset or invoice.amount != requested:
                raise RailError("tgwallet", "created order amount or currency mismatch")
        except RailError as exc:
            raise RailError(
                "tgwallet", "created order cannot be safely bound", outcome_unknown=True
            ) from exc
        return invoice

    async def get_invoice(self, provider_invoice_id: str) -> RailInvoice:
        data = await self._api("GET", "/order/preview", {"id": provider_invoice_id})
        invoice = self._invoice(data)
        if invoice.provider_invoice_id != provider_invoice_id:
            raise RailError("tgwallet", "order identity mismatch", permanent=True)
        return invoice

    async def verify_payment(self, provider_invoice_id: str) -> RailPayment | None:
        invoice = await self.get_invoice(provider_invoice_id)
        if invoice.status != RailInvoiceStatus.PAID:
            return None
        # Preview contains requested amount. Only reconciliation contains the
        # actual paid currency, net settlement and provider fee.
        totals = await self._api("GET", "/reconciliation/order-amount")
        total = totals.get("totalAmount")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise RailError("tgwallet", "malformed reconciliation count")
        # A finite snapshot of the full store replaces a fixed first-N cutoff.
        # The API supports pages up to 10,000 orders.
        page_size = 10000
        for offset in range(0, total, page_size):
            data = await self._api(
                "GET", "/reconciliation/order-list", {"offset": offset, "count": page_size}
            )
            items = data.get("items")
            if not isinstance(items, list):
                raise RailError("tgwallet", "malformed reconciliation list")
            matches = [
                item
                for item in items
                if isinstance(item, dict) and str(item.get("id")) == provider_invoice_id
            ]
            if len(matches) > 1:
                raise RailError("tgwallet", "ambiguous order settlement")
            if matches:
                item = matches[0]
                option = item.get("selectedPaymentOption")
                if item.get("status") != "PAID" or not isinstance(option, dict):
                    raise RailError("tgwallet", "paid preview lacks final settlement evidence")
                paid_asset, paid = self._money(option.get("amount"))
                net_asset, net = self._money(option.get("amountNet"))
                fee_asset, fee = self._money(option.get("amountFee"), positive=False)
                order_asset, order_amount = self._money(item.get("amount"))
                if (
                    order_asset != invoice.asset
                    or order_amount != invoice.amount
                    or net_asset != invoice.asset
                    or fee_asset != net_asset
                ):
                    raise RailError(
                        "tgwallet", "settlement currency or fee mismatch; reconciliation required"
                    )
                canonical_gross = money_sum("tgwallet", net, fee)
                if paid_asset == net_asset:
                    if canonical_gross != paid:
                        raise RailError("tgwallet", "settlement fee does not balance")
                else:
                    # Conversion is creditable only when the provider supplies
                    # actual net and fee in the merchant's canonical currency.
                    # Never compute a guessed net from the order price/rate.
                    if (
                        item.get("autoConversionCurrency") != net_asset
                        or canonical_gross != order_amount
                        or amount(option.get("exchangeRate"), "tgwallet") <= 0
                    ):
                        raise RailError("tgwallet", "canonical conversion evidence unavailable")
                return RailPayment(
                    provider_invoice_id,
                    canonical_gross,
                    net_asset,
                    paid_at=str(item.get("paymentDateTime") or ""),
                    external_id=str(item.get("externalId") or ""),
                    raw=item,
                    settled_amount=net,
                )
            if len(items) < min(page_size, total - offset):
                break
        raise RailError("tgwallet", "paid order settlement not found in reconciliation snapshot")

    async def close(self) -> None:
        await self._http.close()
