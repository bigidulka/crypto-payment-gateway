"""Реестр рельсов: rail_type -> класс. Креды расшифровываются здесь."""

from __future__ import annotations

import json

from src.core.config import get_settings
from src.crypto.encryption import decrypt_secret, encrypt_secret
from src.payments.rails.base import Rail, RailType
from src.payments.rails.cryptobot import CryptobotRail

_RAIL_CLASSES: dict[RailType, type[Rail]] = {
    RailType.CRYPTOBOT: CryptobotRail,
}

_CREDENTIAL_KEYS: dict[RailType, frozenset[str]] = {
    RailType.CRYPTOBOT: frozenset({"token"}),
}


def encrypt_credentials(credentials: dict) -> str:
    """Зашифровать JSON-креды рельса для записи в БД."""
    return encrypt_secret(json.dumps(credentials), _encryption_key()).hex()


def _encryption_key() -> str:
    return get_settings().encryption_key.get_secret_value()


def build_rail(
    rail_type: str,
    encrypted_credentials: str | None,
    *,
    network: str | None = None,
) -> Rail:
    """
    Собрать рельс по типу и зашифрованным кредам.

    Общие on-chain рельсы (EVM) кредов не имеют - encrypted_credentials None.
    """
    try:
        rail_type_enum = RailType(rail_type)
        rail_class = _RAIL_CLASSES[rail_type_enum]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unsupported or unregistered rail type: {rail_type}") from exc

    credentials: dict = {}
    if encrypted_credentials:
        decrypted = decrypt_secret(
            bytes.fromhex(encrypted_credentials), _encryption_key()
        )
        decoded = json.loads(decrypted)
        if not isinstance(decoded, dict):
            raise ValueError("rail credentials must be a JSON object")
        allowed_keys = _CREDENTIAL_KEYS.get(rail_type_enum, frozenset())
        unknown_keys = set(decoded) - allowed_keys
        if unknown_keys:
            raise ValueError(
                f"credentials contain forbidden keys: {', '.join(sorted(unknown_keys))}"
            )
        if "token" not in decoded or not isinstance(decoded["token"], str) or not decoded["token"]:
            raise ValueError("cryptobot credentials require a non-empty token")
        credentials = decoded
    if network is None:
        return rail_class(**credentials) if credentials else rail_class()
    return (
        rail_class(network=network, **credentials)
        if credentials
        else rail_class(network=network)
    )


def register_rail(rail_type: RailType, rail_class: type[Rail]) -> None:
    _RAIL_CLASSES[rail_type] = rail_class
