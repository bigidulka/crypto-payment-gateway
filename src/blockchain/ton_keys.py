"""Real wallet StateInit, not a hash approximation. BIP39 derivation is unchanged."""

from nacl.signing import SigningKey
from tonsdk.contract.wallet import WalletV3ContractR2, WalletV4ContractR2
from tonsdk.utils import Address

from src.blockchain.prepared import ChainEvidenceError


def normalize_ton_address(value: str) -> str:
    try:
        address = Address(value)
        if address.wc not in (0, -1):
            raise ChainEvidenceError("Unsupported TON workchain")
        return address.to_string(False).lower()
    except Exception:
        # tonsdk raises its own generic Exception for malformed checksums.
        raise ChainEvidenceError("Invalid TON address") from None


def wallet_contract(public: bytes, private: bytes | None, version: str = "v3r2", wc: int = 0):
    classes = {"v3r2": WalletV3ContractR2, "v4r2": WalletV4ContractR2}
    if version not in classes or wc not in (0, -1) or len(public) != 32:
        raise ChainEvidenceError("Invalid TON wallet configuration")
    return classes[version](public_key=public, private_key=private, wc=wc)


def wallet_from_seed(value: str, version: str = "v3r2", wc: int = 0):
    raw = bytes.fromhex(value)
    if len(raw) not in (32, 64):
        raise ChainEvidenceError("TON signing key must have 32 or 64 bytes")
    public = bytes(SigningKey(raw[:32]).verify_key)
    if len(raw) == 64 and raw[32:] != public:
        raise ChainEvidenceError("TON keypair public key differs")
    return wallet_contract(public, raw[:32] + public, version, wc)
