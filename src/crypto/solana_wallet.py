"""
Solana HD Wallet для генерации депозитных адресов.

Используется BIP-44 derivation path:
m/44'/501'/0'/0'

где:
- 44' = BIP-44
- 501' = Solana
- 0' = account
- 0' = change (Solana использует hardened)
"""

import hashlib
import hmac
from dataclasses import dataclass

from mnemonic import Mnemonic

# Для Solana используем ed25519
try:
    from nacl.signing import SigningKey
    from nacl.public import PublicKey as NaClPublicKey
    import base58
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

# BIP-44 constants
BIP44_PURPOSE = 44
SOLANA_COIN_TYPE = 501
HARDENED_OFFSET = 0x80000000

# Derivation path template (Solana использует все hardened paths)
DERIVATION_PATH_TEMPLATE = "m/44'/501'/0'/0'"


@dataclass
class SolanaDerivedKey:
    """Результат деривации Solana ключа."""

    address: str  # Base58 encoded public key
    private_key: str  # Base58 encoded private key
    private_key_bytes: bytes  # Raw 64 bytes (seed + public)
    derivation_path: str
    index: int


class SolanaHDWallet:
    """
    HD Wallet для генерации Solana адресов.

    Использует BIP-39 мнемонику и ed25519 для ключей.
    """

    def __init__(self, mnemonic: str) -> None:
        """
        Инициализация кошелька.

        Args:
            mnemonic: BIP-39 мнемоническая фраза (12 или 24 слова)
        """
        if not HAS_NACL:
            raise ImportError(
                "PyNaCl is required for Solana wallet. Install with: pip install pynacl"
            )

        self.mnemonic = mnemonic

        # Валидация мнемоники
        m = Mnemonic("english")
        if not m.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase")

        # Генерируем seed из мнемоники (используем пустой passphrase)
        self.seed = m.to_seed(mnemonic, passphrase="")

    def derive_key(self, index: int) -> SolanaDerivedKey:
        """
        Деривировать ключ по индексу.

        Solana использует: m/44'/501'/index'/0'
        """
        # Derivation path
        path = f"m/44'/501'/{index}'/0'"

        # Деривируем seed для ed25519
        derived_seed = self._derive_ed25519_seed(path)

        # Создаём ключевую пару
        signing_key = SigningKey(derived_seed[:32])
        public_key = signing_key.verify_key

        # Solana private key = 64 bytes (32 seed + 32 public)
        private_key_bytes = derived_seed[:32] + bytes(public_key)

        # Base58 encode
        address = base58.b58encode(bytes(public_key)).decode()
        private_key_b58 = base58.b58encode(private_key_bytes).decode()

        return SolanaDerivedKey(
            address=address,
            private_key=private_key_b58,
            private_key_bytes=private_key_bytes,
            derivation_path=path,
            index=index,
        )

    def _derive_ed25519_seed(self, path: str) -> bytes:
        """
        Деривировать ed25519 seed используя SLIP-0010.

        SLIP-0010 определяет ed25519 derivation для BIP-32.
        """
        # Master key derivation
        h = hmac.new(b"ed25519 seed", self.seed, hashlib.sha512).digest()
        key = h[:32]
        chain_code = h[32:]

        # Parse path
        components = path.replace("m/", "").split("/")

        for component in components:
            hardened = component.endswith("'")
            index = int(component.rstrip("'"))

            if hardened:
                index += HARDENED_OFFSET

            # Child key derivation (SLIP-0010)
            data = b"\x00" + key + index.to_bytes(4, "big")
            h = hmac.new(chain_code, data, hashlib.sha512).digest()
            key = h[:32]
            chain_code = h[32:]

        return key

    def get_address(self, index: int) -> str:
        """Получить адрес по индексу."""
        return self.derive_key(index).address

    def get_private_key(self, index: int) -> str:
        """Получить приватный ключ (Base58) по индексу."""
        return self.derive_key(index).private_key

    def get_keypair_bytes(self, index: int) -> bytes:
        """Получить keypair как bytes (для solana-py)."""
        return self.derive_key(index).private_key_bytes


def create_solana_wallet(mnemonic: str) -> SolanaHDWallet:
    """Создать Solana HD wallet из мнемоники."""
    return SolanaHDWallet(mnemonic)


def generate_solana_mnemonic(strength: int = 128) -> str:
    """
    Сгенерировать новую мнемонику для Solana.

    Args:
        strength: 128 для 12 слов, 256 для 24 слов
    """
    m = Mnemonic("english")
    return m.generate(strength=strength)
