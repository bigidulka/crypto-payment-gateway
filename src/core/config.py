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

    # BIP39 мнемоника для HD кошелька (12 или 24 слова)
    hd_wallet_seed: SecretStr = Field(default="", min_length=0)  # Опциональный
    hd_master_seed: SecretStr = Field(default="", min_length=0)  # Hex seed

    # === Blockchain RPC ===
    # Primary RPC (обязательный)
    base_rpc_url: str = "https://mainnet.base.org"
    arb_rpc_url: str = "https://arb1.arbitrum.io/rpc"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    polygon_rpc_url: str = "https://polygon-rpc.com"
    avax_rpc_url: str = "https://api.avax.network/ext/bc/C/rpc"
    optimism_rpc_url: str = "https://mainnet.optimism.io"

    # Additional RPC endpoints (comma-separated, для failover и ротации)
    # Пример: "https://rpc1.example.com,https://rpc2.example.com"
    base_rpc_urls: str = ""
    arb_rpc_urls: str = ""
    bsc_rpc_urls: str = ""
    polygon_rpc_urls: str = ""
    avax_rpc_urls: str = ""
    optimism_rpc_urls: str = ""

    # RPC rotation strategy: failover, round_robin, latency, weighted
    rpc_rotation_strategy: str = "failover"

    # === Treasury & Funding ===
    treasury_address: str = ""  # Единый treasury адрес
    base_treasury_address: str = ""  # Treasury per chain (fallback)
    arb_treasury_address: str = ""
    bsc_treasury_address: str = ""
    funder_private_key: SecretStr = Field(default="")  # Для gas funding при sweep

    # === Hosted ===
    hosted_base_url: str = "http://localhost:8000"

    # === Worker ===
    poll_interval_seconds: int = 5
    scan_window_size: int = 2000

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
        if self.treasury_address:
            return self.treasury_address

        per_chain = {
            "base": self.base_treasury_address,
            "arbitrum": self.arb_treasury_address,
            "bsc": self.bsc_treasury_address,
        }
        return per_chain.get(chain.lower(), self.treasury_address)

    def get_rpc_urls(self, chain: str) -> list[str]:
        """
        Получить все RPC URLs для сети (primary + additional).

        Returns:
            Список RPC URLs в порядке приоритета
        """
        chain = chain.lower()

        # Primary RPC
        primary_map = {
            "base": self.base_rpc_url,
            "arbitrum": self.arb_rpc_url,
            "bsc": self.bsc_rpc_url,
            "polygon": self.polygon_rpc_url,
            "avax": self.avax_rpc_url,
            "optimism": self.optimism_rpc_url,
        }

        # Additional RPCs (comma-separated)
        additional_map = {
            "base": self.base_rpc_urls,
            "arbitrum": self.arb_rpc_urls,
            "bsc": self.bsc_rpc_urls,
            "polygon": self.polygon_rpc_urls,
            "avax": self.avax_rpc_urls,
            "optimism": self.optimism_rpc_urls,
        }

        urls = []

        # Primary first
        if primary := primary_map.get(chain):
            urls.append(primary)

        # Additional RPCs
        if additional := additional_map.get(chain):
            for url in additional.split(","):
                url = url.strip()
                if url and url not in urls:
                    urls.append(url)

        return urls


@lru_cache
def get_settings() -> Settings:
    """Получение настроек (кешируется)."""
    return Settings()
