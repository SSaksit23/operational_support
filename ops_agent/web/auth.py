"""Sign-in for the hosted app: Firebase Authentication (Google accounts), checked on every /api call.

Off unless OPS_FIREBASE_PROJECT is set, so the laptop install keeps working without sign-in.

  OPS_FIREBASE_PROJECT     the Firebase project id, e.g. operation-tools-330d3
  OPS_FIREBASE_WEB_CONFIG  the web app config JSON from Firebase (apiKey, authDomain, projectId, appId)
  OPS_ALLOWED_EMAILS       comma separated addresses that may sign in
  OPS_ALLOWED_DOMAINS      comma separated domains that may sign in, e.g. 2ucenter.com

With sign-in on and nobody allowed, every request is refused.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

OPEN_PATHS = {"/api/config"}
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def project() -> Optional[str]:
    return os.environ.get("OPS_FIREBASE_PROJECT") or None


def web_config() -> Optional[dict]:
    raw = os.environ.get("OPS_FIREBASE_WEB_CONFIG")
    return json.loads(raw) if raw and project() else None


def _split(name: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


def allowed(email: Optional[str]) -> bool:
    if not email:
        return False
    email = email.lower()
    return email in _split("OPS_ALLOWED_EMAILS") or email.rsplit("@", 1)[-1] in _split("OPS_ALLOWED_DOMAINS")


def _verify(token: str) -> dict:
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token
    return id_token.verify_firebase_token(token, g_requests.Request(), audience=project())


def check(token: Optional[str]) -> tuple[int, Optional[dict], str]:
    """(status, claims, reason). status 200 means let it through."""
    if not project():
        return 200, None, ""
    if not token:
        return 401, None, "กรุณาเข้าสู่ระบบ · Please sign in"
    now = time.time()
    with _lock:
        hit = _cache.get(token)
    if hit and hit[0] > now:
        claims = hit[1]
    else:
        try:
            claims = _verify(token)
        except Exception:  # expired, wrong project, bad signature
            return 401, None, "การเข้าสู่ระบบหมดอายุ กรุณาเข้าใหม่ · Sign-in expired"
        with _lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[token] = (min(claims.get("exp", now + 300), now + 600), claims)
    if not claims.get("email_verified") or not allowed(claims.get("email")):
        return 403, claims, f"{claims.get('email')} ไม่มีสิทธิ์ใช้งาน · This account is not allowed"
    return 200, claims, ""
