"""Flag generation, lookup digesting, and encryption."""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet


class FlagCrypto:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError("A non-empty A&D flag secret is required.")
        self._secret = secret.encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(self._secret).digest())
        self._fernet = Fernet(key)

    def generate(self) -> str:
        return f"FITCTF{{{secrets.token_urlsafe(24)}}}"

    def digest(self, value: str) -> str:
        return hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
