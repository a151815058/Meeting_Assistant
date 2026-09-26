"""Field-level encryption for sensitive DB columns (e.g. OAuth refresh tokens).

Uses Fernet (AES-128-CBC + HMAC) with the key from TOKEN_ENCRYPTION_KEY.
Never log or return decrypted values outside of the request that needs them.
"""
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class TokenCipherError(RuntimeError):
    pass


def _get_fernet() -> Fernet:
    key = current_app.config.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise TokenCipherError("TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    if not ciphertext:
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenCipherError("Unable to decrypt stored token") from exc
