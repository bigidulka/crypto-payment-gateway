import logging
from web3 import Web3
from eth_account import Account
from app.core.config import settings
from app.core.constants import TOKEN_ADDRESSES, ERC20_ABI, NATIVE_SYMBOLS
from app.models import ChainType

# Настройка логирования
logger = logging.getLogger(__name__)

class BlockchainService:
    def __init__(self):
        # Инициализация подключений к нодам
        self.connections = {
            ChainType.BNB: Web3(Web3.HTTPProvider(settings.BNB_RPC_URL, request_kwargs={'proxies': {'https': settings.PROXY_URL, 'http': settings.PROXY_URL}} if settings.PROXY_URL else {})),
            ChainType.BASE: Web3(Web3.HTTPProvider(settings.BASE_RPC_URL, request_kwargs={'proxies': {'https': settings.PROXY_URL, 'http': settings.PROXY_URL}} if settings.PROXY_URL else {})),
            ChainType.ARBITRUM: Web3(Web3.HTTPProvider(settings.ARBITRUM_RPC_URL, request_kwargs={'proxies': {'https': settings.PROXY_URL, 'http': settings.PROXY_URL}} if settings.PROXY_URL else {})),
        }
        
        # Проверка подключений
        for chain, w3 in self.connections.items():
            if w3.is_connected():
                logger.info(f"Connected to {chain}")
            else:
                logger.error(f"Failed to connect to {chain}")

    def create_wallet(self):
        """Генерирует новый кошелек (адрес + приватный ключ)."""
        account = Account.create()
        return account.address, account.key.hex()

    def get_balance(self, chain: ChainType, address: str, token_symbol: str = None):
        """Получает баланс нативного токена или ERC20."""
        w3 = self.connections[chain]
        address = w3.to_checksum_address(address)

        if not token_symbol or token_symbol == NATIVE_SYMBOLS[chain]:
            # Нативный баланс (ETH/BNB)
            balance_wei = w3.eth.get_balance(address)
            return w3.from_wei(balance_wei, 'ether')
        else:
            # ERC20 баланс
            token_address = TOKEN_ADDRESSES.get(chain, {}).get(token_symbol)
            if not token_address:
                raise ValueError(f"Token {token_symbol} not supported on {chain}")
            
            contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
            balance_wei = contract.functions.balanceOf(address).call()
            # Упрощение: считаем что у всех стейблов 18 decimals, хотя у USDC 6. 
            # Для MVP можно хардкодить или проверять decimals.
            # Для примера возьмем 18, но для USDC на Base это 6.
            decimals = 18
            if token_symbol == "USDC": decimals = 6
            
            return balance_wei / (10 ** decimals)

    def send_gas(self, chain: ChainType, to_address: str, amount_eth: float):
        """Отправляет нативный токен (газ) с мастер-кошелька на временный кошелек."""
        w3 = self.connections[chain]
        from_address = w3.to_checksum_address(settings.MASTER_WALLET_ADDRESS)
        to_address = w3.to_checksum_address(to_address)
        
        nonce = w3.eth.get_transaction_count(from_address)
        
        tx = {
            'nonce': nonce,
            'to': to_address,
            'value': w3.to_wei(amount_eth, 'ether'),
            'gas': 21000,
            'gasPrice': w3.eth.gas_price,
            'chainId': w3.eth.chain_id
        }
        
        signed_tx = w3.eth.account.sign_transaction(tx, settings.MASTER_WALLET_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return w3.to_hex(tx_hash)

    def sweep_tokens(self, chain: ChainType, wallet_private_key: str, token_symbol: str, amount: float):
        """Переводит все токены с временного кошелька на мастер-кошелек."""
        w3 = self.connections[chain]
        account = Account.from_key(wallet_private_key)
        from_address = account.address
        to_address = w3.to_checksum_address(settings.MASTER_WALLET_ADDRESS)
        
        token_address = TOKEN_ADDRESSES.get(chain, {}).get(token_symbol)
        if not token_address:
             raise ValueError(f"Token {token_symbol} not supported on {chain}")

        contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
        
        decimals = 18
        if token_symbol == "USDC": decimals = 6
        amount_wei = int(amount * (10 ** decimals))

        # Оценка газа для трансфера
        # Обычно transfer стоит около 60k газа
        gas_limit = 100000 
        
        nonce = w3.eth.get_transaction_count(from_address)
        
        # Строим транзакцию вызова метода transfer
        tx = contract.functions.transfer(to_address, amount_wei).build_transaction({
            'chainId': w3.eth.chain_id,
            'gas': gas_limit,
            'gasPrice': w3.eth.gas_price,
            'nonce': nonce,
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, wallet_private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return w3.to_hex(tx_hash)

    def check_tx_status(self, chain: ChainType, tx_hash: str):
        """Проверяет статус транзакции."""
        w3 = self.connections[chain]
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt and receipt.status == 1:
                return True
            return False
        except Exception:
            return False

blockchain_service = BlockchainService()
