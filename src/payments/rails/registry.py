"""Реестр рельсов: rail_type -> класс. Креды расшифровываются здесь."""

from __future__ import annotations

import json
from typing import Type

from src.core.config import get_settings
from src.crypto.encryption import decrypt_secret, encrypt_secret
from src.payments.rails.base import Rail, RailType
from src.payments.rails.cryptobot import CryptobotRail

_RAIL_CLASSES: dict[RailType, Type[Rail]] = {
    RailType.CRYPTOBOT: CryptobotRail,
}


def encrypt_credentials(credentials: dict) -> str:
    """Зашифровать JSON-креды рельса для записи в БД."""
    return encrypt_secret(json.dumps(credentials), _encryption_key()).hex()


def _encryption_key() -> str:
    return get_settings().encryption_key.get_secret_value()


def build_rail(rail_type: str, encrypted_credentials: str | None, *, network: str | None = None) -> Rail:
    """
    Собрать рельс по типу и зашифрованным кредам.

    Общие on-chain рельсы (EVM) кредов не имеют - encrypted_credentials None.
    """
    try:
        rail_class = _RAIL_CLASSES[RailType(rail_type)]
    except ValueError as exc:
        raise ValueError(f"unknown rail type: {rail_type}") from exc

    credentials: dict = {}
    if encrypted_credentials:
        decrypted = decrypt_secret(
            bytes.fromhex(encrypted_credentials), _encryption_key()
        )
        credentials = json.loads(decrypted)
    return rail_class(network=network, **credentials) if credentials else rail_class()


def register_rail(rail_type: RailType, rail_class: Type[Rail]) -> None:
    _RAIL_CLASSES[rail_type] = rail_class
