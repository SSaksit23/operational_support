"""Encryption for personal IDs.

The key comes from OPS_AGENT_KEY (a Fernet key: Fernet.generate_key()). The
same key also derives the HMAC used to match a returning customer by
passport without decrypting anything.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from cryptography.fernet import Fernet


class IdVault:
    def __init__(self, key: Optional[bytes] = None):
        key = key or os.environ.get("OPS_AGENT_KEY", "").encode()
        if not key:
            raise RuntimeError("Set OPS_AGENT_KEY to a Fernet key to store personal IDs")
        self._fernet = Fernet(key)
        self._mac_key = hashlib.sha256(b"ops-agent-id-hash:" + key).digest()

    def encrypt(self, value: Optional[str]) -> Optional[bytes]:
        return self._fernet.encrypt(value.encode()) if value else None

    def decrypt(self, token: Optional[bytes]) -> Optional[str]:
        return self._fernet.decrypt(bytes(token)).decode() if token else None

    def lookup_hash(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        norm = value.strip().upper().replace(" ", "")
        return hmac.new(self._mac_key, norm.encode(), hashlib.sha256).hexdigest()
