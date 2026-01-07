"""
Public API Router - эндпоинты без авторизации.

Информация о поддерживаемых сетях и токенах.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from src.blockchain.chains import CHAINS_CONFIG

router = APIRouter(prefix="/public", tags=["Public"])


# === Schemas ===


class TokenInfo(BaseModel):
    """Информация о токене."""

    symbol: str
    contract_address: str
    decimals: int
    variant: str  # native | bridged | wrapped


class ChainInfo(BaseModel):
    """Информация о сети."""

    chain_id: int
    name: str
    native_symbol: str
    explorer_url: str
    confirmations: int
    block_time_sec: float
    tokens: list[TokenInfo]


class SupportedChainsResponse(BaseModel):
    """Ответ со списком поддерживаемых сетей."""

    chains: dict[str, ChainInfo]
    total_chains: int
    total_tokens: int


class TokenListItem(BaseModel):
    """Элемент списка токенов."""

    chain: str
    chain_name: str
    chain_id: int
    symbol: str
    contract_address: str
    decimals: int
    explorer_url: str


class TokenListResponse(BaseModel):
    """Ответ со списком всех токенов."""

    tokens: list[TokenListItem]
    total: int


# === Endpoints ===


@router.get(
    "/chains",
    response_model=SupportedChainsResponse,
    summary="Список поддерживаемых сетей",
    description="Возвращает все поддерживаемые блокчейн сети с токенами и контрактами",
)
async def get_supported_chains() -> SupportedChainsResponse:
    """
    Получить список всех поддерживаемых сетей.

    Для каждой сети возвращается:
    - chain_id: ID сети
    - name: Название сети
    - native_symbol: Нативный токен (ETH, BNB, MATIC, AVAX)
    - explorer_url: URL блок-эксплорера
    - confirmations: Требуемое количество подтверждений
    - tokens: Список поддерживаемых токенов с контрактами
    """
    chains = {}
    total_tokens = 0

    for chain_key, config in CHAINS_CONFIG.items():
        tokens = []
        for token_config in config.tokens.values():
            tokens.append(
                TokenInfo(
                    symbol=token_config.symbol,
                    contract_address=token_config.contract_address,
                    decimals=token_config.decimals,
                    variant=token_config.variant,
                )
            )
            total_tokens += 1

        chains[chain_key] = ChainInfo(
            chain_id=config.chain_id,
            name=config.name,
            native_symbol=config.native_symbol,
            explorer_url=config.explorer_url,
            confirmations=config.confirmations,
            block_time_sec=config.block_time_sec,
            tokens=tokens,
        )

    return SupportedChainsResponse(
        chains=chains,
        total_chains=len(chains),
        total_tokens=total_tokens,
    )


@router.get(
    "/tokens",
    response_model=TokenListResponse,
    summary="Список всех токенов",
    description="Возвращает плоский список всех токенов со всех сетей",
)
async def get_all_tokens() -> TokenListResponse:
    """
    Получить плоский список всех токенов.

    Удобно для отображения в UI - все токены в одном списке
    с указанием сети и адреса контракта.
    """
    tokens = []

    for chain_key, config in CHAINS_CONFIG.items():
        for token_config in config.tokens.values():
            tokens.append(
                TokenListItem(
                    chain=chain_key,
                    chain_name=config.name,
                    chain_id=config.chain_id,
                    symbol=token_config.symbol,
                    contract_address=token_config.contract_address,
                    decimals=token_config.decimals,
                    explorer_url=f"{config.explorer_url}/token/{token_config.contract_address}",
                )
            )

    return TokenListResponse(
        tokens=tokens,
        total=len(tokens),
    )


@router.get(
    "/chain/{chain}",
    response_model=ChainInfo,
    summary="Информация о сети",
    description="Возвращает информацию о конкретной сети",
)
async def get_chain_info(chain: str) -> ChainInfo:
    """
    Получить информацию о конкретной сети.

    Args:
        chain: Идентификатор сети (arbitrum, base, bsc, polygon, avax, optimism)
    """
    from fastapi import HTTPException

    chain = chain.lower()
    if chain not in CHAINS_CONFIG:
        raise HTTPException(
            status_code=404,
            detail=f"Chain '{chain}' not supported. Available: {list(CHAINS_CONFIG.keys())}",
        )

    config = CHAINS_CONFIG[chain]
    tokens = [
        TokenInfo(
            symbol=t.symbol,
            contract_address=t.contract_address,
            decimals=t.decimals,
            variant=t.variant,
        )
        for t in config.tokens.values()
    ]

    return ChainInfo(
        chain_id=config.chain_id,
        name=config.name,
        native_symbol=config.native_symbol,
        explorer_url=config.explorer_url,
        confirmations=config.confirmations,
        block_time_sec=config.block_time_sec,
        tokens=tokens,
    )


@router.get(
    "/health",
    summary="Health check",
    description="Проверка работоспособности API",
)
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "arbitron-payment",
        "chains_available": len(CHAINS_CONFIG),
    }
