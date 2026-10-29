"""Versioned custodial asset precision; amount conversion never rounds."""

import tomllib
from functools import lru_cache
from pathlib import Path

CUSTODIAL_RAIL_TYPES = frozenset({"cryptobot", "xrocket", "tgwallet"})


def gateway_onchain_enabled(merchant_id, settings=None, chain=None):
    from src.core.config import get_settings

    settings = settings or get_settings()
    chains = {item.strip() for item in settings.onchain_gateway_chains.split(",") if item.strip()}
    # A staged rollout keeps the other chains of the merchant on their current path.
    if chain is not None and chains and chain not in chains:
        return False
    return (
        settings.onchain_gateway_enabled
        and settings.ledger_rail_orchestration_enabled
        and str(merchant_id) in settings.ledger_rail_orchestration_merchant_ids
    )


def invoice_checkout_chains(invoice):
    metadata = getattr(invoice, "extra_data", None) or {}
    if metadata.get("gateway_receipt_required") is True and not gateway_onchain_enabled(
        invoice.merchant_id
    ):
        return []
    return list(invoice.allowed_chains)


@lru_cache(maxsize=1)
def provider_asset_decimals() -> dict[str, dict[str, int]]:
    path = Path(__file__).resolve().parents[2] / "config" / "providers.toml"
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    result = {}
    for name in CUSTODIAL_RAIL_TYPES:
        assets = config[name]["assets"]
        if not assets or any(
            not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 18
            for v in assets.values()
        ):
            raise ValueError("invalid custodial asset precision configuration")
        result[name] = dict(assets)
    return result


def asset_decimals(rail_type: str, symbol: str) -> int:
    try:
        return provider_asset_decimals()[rail_type][symbol]
    except KeyError as exc:
        raise ValueError("asset is not configured for this provider") from exc


def provider_assets() -> set[str]:
    return {symbol for symbols in provider_asset_decimals().values() for symbol in symbols}
