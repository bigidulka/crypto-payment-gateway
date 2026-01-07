"""
Модуль безопасности для депозитов.

Защита от:
1. Dust attacks (микро-транзакции для спама)
2. Phishing/Poison attacks (фейковые токены с похожими адресами)
3. Double-crediting (двойное зачисление на баланс)
4. Zero-amount transfers
5. Аномально большие суммы
"""

import logging
from decimal import Decimal
from typing import NamedTuple

from src.blockchain.chains import get_chain_config
from src.core.config import get_settings

logger = logging.getLogger(__name__)


class ValidationResult(NamedTuple):
    """Результат валидации депозита."""

    is_valid: bool
    reason: str | None = None


# Известные скам-токены (адреса в lowercase)
# Добавляйте сюда токены которые маскируются под USDT/USDC
KNOWN_SCAM_TOKENS: set[str] = {
    # Примеры фейковых USDT на разных сетях
    # Добавляйте по мере обнаружения
}


def validate_deposit(
    chain: str,
    token_contract: str,
    amount: Decimal,
    asset: str,
    from_address: str | None = None,
) -> ValidationResult:
    """
    Валидировать депозит на предмет безопасности.

    Args:
        chain: Название сети
        token_contract: Адрес контракта токена
        amount: Сумма депозита
        asset: USDT или USDC
        from_address: Адрес отправителя (опционально)

    Returns:
        ValidationResult с результатом проверки
    """
    settings = get_settings()
    config = get_chain_config(chain)

    # === 1. Проверка на нулевую сумму ===
    if amount <= 0:
        logger.warning(
            f"[SECURITY] Zero/negative amount deposit rejected: "
            f"{amount} {asset} on {chain}"
        )
        return ValidationResult(False, "zero_amount")

    # === 2. Проверка минимальной суммы (anti-dust) ===
    min_amount = Decimal(
        settings.min_deposit_usdt if asset == "USDT" else settings.min_deposit_usdc
    )
    if amount < min_amount:
        logger.warning(
            f"[SECURITY] Dust deposit rejected: {amount} {asset} < {min_amount} on {chain}"
        )
        return ValidationResult(False, "below_minimum")

    # === 3. Проверка максимальной суммы (anomaly detection) ===
    max_amount = Decimal(
        settings.max_deposit_usdt if asset == "USDT" else settings.max_deposit_usdc
    )
    if amount > max_amount:
        logger.warning(
            f"[SECURITY] Anomaly: deposit {amount} {asset} > {max_amount} on {chain}"
        )
        # Не отклоняем, но логируем для ручной проверки
        # В production можно добавить флаг requires_review

    # === 4. Строгая проверка адреса контракта токена ===
    token_contract_lower = token_contract.lower()

    # Получаем официальные адреса токенов для сети
    official_contracts = {
        token.contract_address.lower(): name for name, token in config.tokens.items()
    }

    if token_contract_lower not in official_contracts:
        logger.error(
            f"[SECURITY] PHISHING ATTEMPT! Unknown token contract: "
            f"{token_contract} claiming to be {asset} on {chain}"
        )
        return ValidationResult(False, "unknown_token_contract")

    # Проверяем что asset соответствует контракту
    actual_asset = official_contracts[token_contract_lower]
    if actual_asset != asset:
        logger.error(
            f"[SECURITY] Token mismatch! Contract {token_contract} "
            f"is {actual_asset} but claimed as {asset}"
        )
        return ValidationResult(False, "asset_mismatch")

    # === 5. Проверка на известные скам-токены ===
    if token_contract_lower in KNOWN_SCAM_TOKENS:
        logger.error(f"[SECURITY] SCAM TOKEN DETECTED: {token_contract} on {chain}")
        return ValidationResult(False, "known_scam_token")

    # === 6. Проверка from_address (опционально) ===
    if from_address:
        from_lower = from_address.lower()

        # Проверка на self-transfer (странно, но не блокируем)
        # Можно добавить blacklist адресов
        pass

    return ValidationResult(True, None)


def is_already_credited(deposit) -> bool:
    """
    Проверить, был ли депозит уже зачислен на баланс.

    Защита от double-crediting при race conditions.
    """
    return deposit.credited_at is not None


def validate_token_contract_strict(
    chain: str,
    token_contract: str,
) -> tuple[bool, str | None]:
    """
    Строгая проверка что token_contract - это наш официальный токен.

    Returns:
        (is_valid, asset_name) - если валидный, возвращает имя актива
    """
    config = get_chain_config(chain)
    token_contract_lower = token_contract.lower()

    for asset_name, token in config.tokens.items():
        if token.contract_address.lower() == token_contract_lower:
            return True, asset_name

    return False, None
