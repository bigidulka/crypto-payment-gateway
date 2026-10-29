"""Framework-independent product integration lives in the installable SDK.

Use ProductPayments.create_order() from your trusted purchase handler. Expose
receive_webhook() on your HTTP endpoint or call reconcile_order() from a bounded
scheduler. Fulfill pending_fulfillments() idempotently by order_id, then call
acknowledge_fulfillment(). No CryptoBot/xRocket libraries or credentials belong
in the product. The product owns pricing, subscription rules and delivery.
"""

from arbitron_sdk import ProductPayments

__all__ = ["ProductPayments"]
