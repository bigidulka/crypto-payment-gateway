"""Durable checkout and fulfillment inbox for framework-independent products.

Only the gateway key is needed. Provider credentials and chain signing stay in
the gateway. A product must fulfill each pending item idempotently by order ID
before acknowledging it; an external side effect cannot share this SQLite
transaction and is intentionally never acknowledged automatically.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from uuid import UUID

from .client import ArbitronClient
from .models import Invoice, InvoiceStatus
from .webhooks import verify_webhook


class ProductPayments:
    """One persistent database and one merchant identity per product."""

    def __init__(
        self, client: ArbitronClient, database: Path, webhook_secret: str = ""
    ) -> None:
        self.client = client
        self.database = Path(database)
        self.webhook_secret = webhook_secret
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS checkout_orders (
                    id TEXT PRIMARY KEY, customer TEXT NOT NULL,
                    amount TEXT NOT NULL, asset TEXT NOT NULL,
                    chains TEXT NOT NULL, rails TEXT NOT NULL,
                    ttl INTEGER NOT NULL, telegram_id TEXT NOT NULL,
                    invoice_id TEXT UNIQUE, public_id TEXT, hosted_url TEXT,
                    event_id TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS checkout_fulfillments (
                    order_id TEXT PRIMARY KEY REFERENCES checkout_orders(id),
                    event_id TEXT NOT NULL UNIQUE, receipt TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _amount(value: object) -> Decimal:
        if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, str, int)):
            raise ValueError("Use an exact decimal amount")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid exact amount") from exc
        if (not amount.is_finite() or len(amount.as_tuple().digits) > 78
                or not -78 <= amount.adjusted() <= 77):
            raise ValueError("Amount must be finite and fit gateway precision")
        return amount

    def _order(self, order_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkout_orders WHERE id=?", (order_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown product order")
        return dict(row)

    @staticmethod
    def _check_invoice(row: dict, invoice: Invoice) -> None:
        if (
            not invoice.invoice_id
            or not invoice.public_id
            or row["invoice_id"] not in (None, invoice.invoice_id)
            or row["public_id"] not in (None, invoice.public_id)
            or invoice.amount != Decimal(row["amount"])
            or invoice.asset != row["asset"]
            or set(invoice.allowed_chains) != set(json.loads(row["chains"]))
            or set(invoice.allowed_rails) != set(json.loads(row["rails"]))
            or invoice.metadata.get("order_id") != row["id"]
            or invoice.metadata.get("external_user_id") != row["customer"]
            or invoice.metadata.get("gateway_receipt_required") is not True
        ):
            raise ValueError("Gateway invoice does not match the product order")

    async def create_order(
        self,
        order_id: UUID,
        customer: str,
        amount: Decimal | str,
        asset: str,
        rail_id: str = "",
        *,
        allowed_chains: tuple[str, ...] | list[str] = (),
        allowed_rails: tuple[str, ...] | list[str] = (),
        ttl_minutes: int = 60,
        customer_telegram_id: str = "",
    ) -> dict:
        """Persist the trusted order before HTTP; reuse its ID after uncertainty."""
        key = str(UUID(str(order_id)))
        value = self._amount(amount)
        asset = asset.strip().upper()
        chains = sorted(set(allowed_chains))
        rails = sorted(set([rail_id] if rail_id else allowed_rails))
        if (
            value <= 0 or not customer or not asset or not (chains or rails)
            or isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int)
            or not 5 <= ttl_minutes <= 1440
            or (rail_id and allowed_rails)
            or any(not isinstance(item, str) or not item for item in [*chains, *rails])
        ):
            raise ValueError("Trusted customer, exact positive price and payment methods required")
        values = (customer, str(value), asset, json.dumps(chains), json.dumps(rails),
                  ttl_minutes, customer_telegram_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO checkout_orders "
                "(id,customer,amount,asset,chains,rails,ttl,telegram_id) VALUES(?,?,?,?,?,?,?,?)",
                (key, *values),
            )
            row = dict(connection.execute(
                "SELECT * FROM checkout_orders WHERE id=?", (key,)
            ).fetchone())
            existing = (row["customer"], row["amount"], row["asset"], row["chains"],
                        row["rails"], row["ttl"], row["telegram_id"])
            if (existing[0] != values[0] or Decimal(existing[1]) != value
                    or existing[2:] != values[2:]):
                raise ValueError("Order ID is bound to another purchase")
        if row["event_id"]:
            return {"order_id": key, "invoice_id": row["invoice_id"], "status": "CONFIRMED"}
        if row["invoice_id"]:
            invoice = await self.client.get_invoice(row["invoice_id"])
        else:
            invoice = await self.client.create_invoice(
                amount=value, asset=asset, allowed_chains=chains, allowed_rails=rails,
                external_user_id=customer, ttl_minutes=ttl_minutes,
                metadata={"order_id": key, "gateway_receipt_required": True},
                idempotency_key=key, customer_telegram_id=customer_telegram_id,
            )
        self._check_invoice(row, invoice)
        with self._connect() as connection:
            # Compare again under the SQLite write lock against a concurrent response.
            connection.execute("BEGIN IMMEDIATE")
            current = dict(connection.execute(
                "SELECT * FROM checkout_orders WHERE id=?", (key,)
            ).fetchone())
            self._check_invoice(current, invoice)
            connection.execute(
                "UPDATE checkout_orders SET invoice_id=?,public_id=?,hosted_url=? WHERE id=?",
                (invoice.invoice_id, invoice.public_id, invoice.hosted_url, key),
            )
        return {"order_id": key, "invoice_id": invoice.invoice_id,
                "hosted_url": invoice.hosted_url, "public_id": invoice.public_id, "status": str(
                    getattr(invoice.status, "value", invoice.status)
                )}

    async def reconcile_order(self, order_id: UUID | str) -> bool:
        """Polling fallback. Missing/unfinished receipts never grant a purchase."""
        row = self._order(str(UUID(str(order_id))))
        if not row["invoice_id"]:
            return False
        invoice = await self.client.get_invoice(row["invoice_id"])
        return await self._confirm(row, invoice)

    async def _confirm(self, row: dict, invoice: Invoice, event_id: str | None = None) -> bool:
        self._check_invoice(row, invoice)
        if invoice.status != InvoiceStatus.CONFIRMED:
            return False
        result = await self.client.invoice_receipt(invoice.invoice_id)
        receipt = result.get("receipt")
        if (result.get("invoice_id") != invoice.invoice_id
                or result.get("status") != "CONFIRMED" or not isinstance(receipt, dict)):
            return False
        proof_event = receipt.get("event_id")
        if not isinstance(proof_event, str) or not proof_event or (
            event_id is not None and event_id != proof_event
        ):
            raise ValueError("Receipt event identity differs")
        gross = self._amount(receipt.get("gross"))
        net = self._amount(receipt.get("net"))
        fee = self._amount(receipt.get("provider_fee"))
        with localcontext() as context:
            context.prec = 160
            balanced = net + fee == gross
        if (receipt.get("asset") != row["asset"] or gross < Decimal(row["amount"])
                or net <= 0 or fee < 0 or not balanced):
            raise ValueError("Invalid confirmed settlement")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = dict(connection.execute(
                    "SELECT * FROM checkout_orders WHERE id=?", (row["id"],)
                ).fetchone())
                self._check_invoice(current, invoice)
                if current["event_id"]:
                    if current["event_id"] != proof_event:
                        raise ValueError("Purchase already has a different settlement")
                    return False
                connection.execute(
                    "UPDATE checkout_orders SET event_id=?,invoice_id=?,public_id=? WHERE id=?",
                    (proof_event, invoice.invoice_id, invoice.public_id, row["id"]),
                )
                connection.execute(
                    "INSERT INTO checkout_fulfillments(order_id,event_id,receipt) VALUES(?,?,?)",
                    (row["id"], proof_event, json.dumps(receipt, sort_keys=True)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Receipt already belongs to another order") from exc
        return True

    async def receive_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self.webhook_secret:
            raise ValueError("Webhook secret is not configured")
        event = verify_webhook(body, headers, self.webhook_secret)
        if event.event_type != "invoice.confirmed":
            return False
        invoice_id = str(UUID(event.data["invoice"]["id"]))
        invoice = await self.client.get_invoice(invoice_id)
        if invoice.invoice_id != invoice_id:
            raise ValueError("Webhook invoice identity differs")
        row = self._order(str(UUID(invoice.metadata["order_id"])))
        event_id = event.data.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Webhook requires a durable event identity")
        return await self._confirm(row, invoice, event_id)

    def pending_fulfillments(self, *, limit: int = 100) -> list[dict]:
        """Replay after restart; fulfill by order_id before acknowledging delivery."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT f.order_id,f.event_id,f.receipt,o.customer,o.amount,o.asset "
                "FROM checkout_fulfillments f JOIN checkout_orders o ON o.id=f.order_id "
                "WHERE f.delivered=0 ORDER BY f.rowid LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "receipt": json.loads(row["receipt"])} for row in rows]

    def acknowledge_fulfillment(self, order_id: UUID | str, event_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE checkout_fulfillments SET delivered=1 WHERE order_id=? AND event_id=?",
                (str(UUID(str(order_id))), event_id),
            )
            if result.rowcount != 1:
                raise ValueError("Unknown fulfillment identity")
