"""
Шифрование приватных ключей с использованием AES-256-GCM.

Схема:
- Ключ шифрования: 32 байта из env (ENCRYPTION_KEY) в base64
- Алгоритм: AES-256-GCM
- Формат зашифрованных данных: nonce (12 bytes) + tag (16 bytes) + ciphertext
"""

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_encryption_key() -> str:
    """
    Сгенерировать новый ключ шифрования.

    Returns:
        32-byte ключ в base64 формате
    """
    key = secrets.token_bytes(32)
    return base64.b64encode(key).decode()


def _get_key_bytes(key_base64: str) -> bytes:
    """Декодировать ключ из base64."""
    key_bytes = base64.b64decode(key_base64)
    if len(key_bytes) != 32:
        raise ValueError(f"Encryption key must be 32 bytes, got {len(key_bytes)}")
    return key_bytes


def encrypt_private_key(private_key: str, encryption_key: str) -> bytes:
    """
    Зашифровать приватный ключ.

    Args:
        private_key: Приватный ключ в hex формате (с или без 0x)
        encryption_key: Ключ шифрования в base64

    Returns:
        Зашифрованные данные: nonce (12) + tag (16) + ciphertext
    """
    # Нормализуем приватный ключ
    if private_key.startswith("0x"):
        private_key = private_key[2:]

    # Конвертируем в байты
    privkey_bytes = bytes.fromhex(private_key)
    key_bytes = _get_key_bytes(encryption_key)

    # Генерируем случайный nonce (12 байт для GCM)
    nonce = secrets.token_bytes(12)

    # Шифруем
    aesgcm = AESGCM(key_bytes)
    ciphertext_with_tag = aesgcm.encrypt(nonce, privkey_bytes, None)

    # Формат: nonce + ciphertext_with_tag
    return nonce + ciphertext_with_tag


def decrypt_private_key(encrypted_data: bytes, encryption_key: str) -> str:
    """
    Расшифровать приватный ключ.

    Args:
        encrypted_data: Зашифрованные данные (nonce + tag + ciphertext)
        encryption_key: Ключ шифрования в base64

    Returns:
        Приватный ключ в hex формате с префиксом 0x

    Raises:
        ValueError: Если расшифровка не удалась (неверный ключ или данные повреждены)
    """
    if len(encrypted_data) < 12 + 16:  # минимум nonce + tag
        raise ValueError("Invalid encrypted data: too short")

    key_bytes = _get_key_bytes(encryption_key)

    # Извлекаем nonce (первые 12 байт)
    nonce = encrypted_data[:12]
    ciphertext_with_tag = encrypted_data[12:]

    # Расшифровываем
    try:
        aesgcm = AESGCM(key_bytes)
        privkey_bytes = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}")

    # Возвращаем в hex формате
    return "0x" + privkey_bytes.hex()


def rotate_encryption_key(
    encrypted_data: bytes,
    old_key: str,
    new_key: str,
) -> bytes:
    """
    Перешифровать данные новым ключом.

    Args:
        encrypted_data: Данные, зашифрованные старым ключом
        old_key: Старый ключ шифрования
        new_key: Новый ключ шифрования

    Returns:
        Данные, зашифрованные новым ключом
    """
    # Расшифровываем старым ключом
    private_key = decrypt_private_key(encrypted_data, old_key)
    # Убираем 0x для encrypt_private_key
    private_key = private_key[2:] if private_key.startswith("0x") else private_key
    # Шифруем новым ключом
    return encrypt_private_key(private_key, new_key)
