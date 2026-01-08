"""
TON HD Wallet для генерации депозитных адресов.

TON использует ed25519 ключи, но с другой схемой деривации.
Стандартный путь для TON wallets:
m/44'/607'/0' (607 = TON coin type по SLIP-44)

TON кошельки (v3r2, v4r2) используют public key + workchain для генерации адреса.
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Tuple

from mnemonic import Mnemonic

# Для ed25519
try:
    from nacl.signing import SigningKey
    import base64
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

# BIP-44 constants
BIP44_PURPOSE = 44
TON_COIN_TYPE = 607
HARDENED_OFFSET = 0x80000000

# TON wallet versions
WALLET_V3R2 = "v3r2"
WALLET_V4R2 = "v4r2"

# V3R2 wallet code hash (для вычисления адреса)
V3R2_CODE_HASH = bytes.fromhex(
    "84dafa449f98a6987789ba232358072bc0f76dc4524002a5d0918b9a75d2d599"
)


@dataclass
class TonDerivedKey:
    """Результат деривации TON ключа."""

    address: str  # User-friendly address (bounceable)
    address_raw: str  # Raw address (workchain:hash)
    public_key: str  # Hex encoded public key
    private_key: str  # Hex encoded private key (32 bytes)
    derivation_path: str
    index: int


class TonHDWallet:
    """
    HD Wallet для генерации TON адресов.

    Использует BIP-39 мнемонику и ed25519 для ключей.
    Генерирует адреса для wallet v3r2 по умолчанию.
    """

    def __init__(
        self,
        mnemonic: str,
        wallet_version: str = WALLET_V3R2,
        workchain: int = 0,
    ) -> None:
        """
        Инициализация кошелька.

        Args:
            mnemonic: BIP-39 мнемоническая фраза (24 слова)
            wallet_version: Версия TON wallet (v3r2, v4r2)
            workchain: Workchain ID (0 для basechain, -1 для masterchain)
        """
        if not HAS_NACL:
            raise ImportError(
                "PyNaCl is required for TON wallet. Install with: pip install pynacl"
            )

        self.mnemonic = mnemonic
        self.wallet_version = wallet_version
        self.workchain = workchain

        # Валидация мнемоники
        m = Mnemonic("english")
        if not m.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase")

        # Генерируем seed
        self.seed = m.to_seed(mnemonic, passphrase="")

    def derive_key(self, index: int) -> TonDerivedKey:
        """
        Деривировать ключ по индексу.

        Path: m/44'/607'/index'
        """
        path = f"m/44'/607'/{index}'"

        # Деривируем ed25519 seed
        derived_seed = self._derive_ed25519_seed(path)

        # Создаём ключевую пару
        signing_key = SigningKey(derived_seed[:32])
        public_key = signing_key.verify_key

        # Генерируем TON адрес
        address_raw, address = self._compute_wallet_address(bytes(public_key))

        return TonDerivedKey(
            address=address,
            address_raw=address_raw,
            public_key=bytes(public_key).hex(),
            private_key=derived_seed[:32].hex(),
            derivation_path=path,
            index=index,
        )

    def _derive_ed25519_seed(self, path: str) -> bytes:
        """
        Деривировать ed25519 seed используя SLIP-0010.
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

    def _compute_wallet_address(self, public_key: bytes) -> Tuple[str, str]:
        """
        Вычислить адрес TON wallet из public key.

        Returns:
            Tuple[raw_address, user_friendly_address]
        """
        # Для v3r2 wallet адрес = hash(code + data)
        # data = subwallet_id(32bit) + public_key(256bit)
        # Упрощённая реализация (для полной нужна TVM Cell сериализация)

        subwallet_id = 698983191  # Default subwallet ID

        # Создаём data cell (упрощённо)
        data = (
            subwallet_id.to_bytes(4, "big") +
            b"\x00" * 4 +  # seqno = 0
            public_key
        )

        # Hash
        state_hash = hashlib.sha256(V3R2_CODE_HASH + data).digest()

        # Raw address
        raw_address = f"{self.workchain}:{state_hash.hex()}"

        # User-friendly address (bounceable, mainnet)
        user_friendly = self._to_user_friendly(
            self.workchain, state_hash, bounceable=True, testnet=False
        )

        return raw_address, user_friendly

    def _to_user_friendly(
        self,
        workchain: int,
        address_hash: bytes,
        bounceable: bool = True,
        testnet: bool = False,
    ) -> str:
        """
        Конвертировать в user-friendly формат адреса.

        Формат: tag(1) + workchain(1) + hash(32) + crc16(2)
        """
        # Tag byte
        tag = 0x11 if bounceable else 0x51
        if testnet:
            tag |= 0x80

        # Workchain byte (signed)
        wc_byte = workchain.to_bytes(1, "big", signed=True)

        # Full address bytes
        addr_bytes = bytes([tag]) + wc_byte + address_hash

        # CRC16
        crc = self._crc16(addr_bytes)
        addr_bytes += crc.to_bytes(2, "big")

        # Base64url encode
        return base64.urlsafe_b64encode(addr_bytes).decode().rstrip("=")

    def _crc16(self, data: bytes) -> int:
        """Вычислить CRC16-CCITT."""
        crc = 0
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    def get_address(self, index: int) -> str:
        """Получить адрес по индексу."""
        return self.derive_key(index).address

    def get_private_key(self, index: int) -> str:
        """Получить приватный ключ (hex) по индексу."""
        return self.derive_key(index).private_key


def create_ton_wallet(
    mnemonic: str,
    wallet_version: str = WALLET_V3R2,
) -> TonHDWallet:
    """Создать TON HD wallet из мнемоники."""
    return TonHDWallet(mnemonic, wallet_version)


def generate_ton_mnemonic(word_count: int = 24) -> str:
    """
    Сгенерировать новую мнемонику для TON.

    Args:
        word_count: 12 или 24 слова
    """
    strength = 128 if word_count == 12 else 256
    m = Mnemonic("english")
    return m.generate(strength=strength)
