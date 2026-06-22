"""
Конфигурация приложения.
Все настройки загружаются из переменных окружения.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Главные настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === App ===
    app_name: str = "ArbitronPayment"
    app_version: str = "0.1.0"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    secret_key: SecretStr = Field(..., min_length=32)

    # === Database ===
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/arbitron_payment"
    )

    # === Redis ===
    redis_url: str = "redis://localhost:6379/0"

    # === Security ===
    # 32-byte ключ в base64 для AES-256-GCM шифрования приватных ключей
    encryption_key: SecretStr = Field(..., min_length=32)

    # Секретный ключ для доступа к админ-панели (минимум 32 символа)
    admin_secret_key: SecretStr = Field(default="", min_length=0)

    # BIP39 мнемоника для HD кошелька (12 или 24 слова)
    hd_wallet_seed: SecretStr = Field(default="", min_length=0)  # Опциональный
    hd_master_seed: SecretStr = Field(default="", min_length=0)  # Hex seed

    # === Non-EVM RPC (пока остаются в env, не мигрировали в TOML) ===
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    ton_rpc_url: str = "https://toncenter.com/api/v2"
    ton_api_key: str = ""  # TON Center API key (опционально)

    # === Treasury & Funding ===
    treasury_address: str = ""  # Единый treasury адрес (EVM)
    base_treasury_address: str = ""  # Treasury per chain (fallback)
    arb_treasury_address: str = ""
    bsc_treasury_address: str = ""
    solana_treasury_address: str = ""  # Solana treasury
    ton_treasury_address: str = ""  # TON treasury
    funder_private_key: SecretStr = Field(default="")  # Для gas funding при sweep

    # === HD Wallet Seeds (per chain type) ===
    # Solana использует отдельный seed (BIP44/501')
    solana_wallet_seed: SecretStr = Field(default="")
    # TON использует отдельный seed (BIP44/607')
    ton_wallet_seed: SecretStr = Field(default="")

    # === Hosted ===
    hosted_base_url: str = "http://localhost:8000"

    # === Worker ===
    poll_interval_seconds: int = 5
    scan_window_size: int = 2000

    # === OKLink Deposit Scanner ===
    oklink_base_url: str = ""
    oklink_api_prefix: str = ""
    oklink_referer: str = ""
    oklink_user_agent: str = ""
    oklink_web_key: SecretStr = Field(default="")
    oklink_api_key_time_shift_ms: int = 0
    oklink_request_timeout_seconds: float = 20.0

    # === Security / Anti-Phishing ===
    # Минимальная сумма депозита (защита от dust/poison attacks)
    min_deposit_usdt: str = "0.01"  # $0.01 минимум
    min_deposit_usdc: str = "0.01"

    # Максимальная сумма одного депозита (аномалия)
    max_deposit_usdt: str = "1000000"  # $1M максимум
    max_deposit_usdc: str = "1000000"

    # === Webhook ===
    webhook_max_attempts: int = 5
    webhook_timeout_seconds: int = 30

    # === CORS ===
    # В production укажите конкретные домены через запятую
    # Пример: "https://example.com,https://api.example.com"
    cors_origins: str = "*"

    @computed_field
    @property
    def is_production(self) -> bool:
        """Проверка на production окружение."""
        return self.app_env == "production"

    @property
    def cors_origins_list(self) -> list[str]:
        """Получить список разрешённых CORS origins."""
        if self.cors_origins == "*":
            return ["*"]
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    def get_treasury_address(self, chain: str) -> str:
        """Получить treasury адрес для сети."""
        chain = chain.lower()

        # Non-EVM сети имеют свои treasury
        if chain == "solana":
            return self.solana_treasury_address or self.treasury_address
        if chain == "ton":
            return self.ton_treasury_address or self.treasury_address

        # EVM treasury
        if self.treasury_address:
            return self.treasury_address

        per_chain = {
            "base": self.base_treasury_address,
            "arbitrum": self.arb_treasury_address,
            "bsc": self.bsc_treasury_address,
        }
        return per_chain.get(chain, self.treasury_address)

    def get_rpc_urls(self, chain: str) -> list[str]:
        """
        Получить все RPC URLs для сети из chains.toml.

        Returns:
            Список RPC URLs в порядке приоритета
        """
        from src.blockchain.chains import get_rpc_urls as get_chain_rpc_urls

        return get_chain_rpc_urls(chain)


@lru_cache
def get_settings() -> Settings:
    """Получение настроек (кешируется)."""
    return Settings()
