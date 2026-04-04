from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Arbitron Payment"
    
    # Database
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_SERVER: str
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str
    
    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # Blockchain
    MASTER_WALLET_PRIVATE_KEY: str  # Приватный ключ главного кошелька для сбора средств
    MASTER_WALLET_ADDRESS: str      # Адрес главного кошелька
    
    # RPC Nodes (можно использовать публичные или свои)
    BNB_RPC_URL: str = "https://bsc-dataseed.binance.org/"
    BASE_RPC_URL: str = "https://mainnet.base.org"
    ARBITRUM_RPC_URL: str = "https://arb1.arbitrum.io/rpc"
    
    # Proxy (SOCKS5)
    PROXY_URL: Optional[str] = None # e.g., socks5://user:pass@host:port

    # Security
    SECRET_KEY: str = "supersecretkeychangeinproduction"

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
