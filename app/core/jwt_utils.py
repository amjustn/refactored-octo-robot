"""Minimal JWT (HS256) using stdlib only — no external dependencies.

Single-user self-hosted use: signs and verifies tokens with the
BERKSHIRE_API_TOKEN as the shared secret.
"""
import base64
import hashlib
import hmac
import json
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s = s.encode("ascii")
    # Add padding
    rem = len(s) % 4
    if rem:
        s += b"=" * (4 - rem)
    return base64.urlsafe_b64decode(s)


def create_token(secret: str, *, ttl: int = 86400) -> str:
    """Create a signed JWT valid for `ttl` seconds (default 24h)."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": "berkshire", "iat": now, "exp": now + ttl}

    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    msg = f"{h}.{p}".encode("ascii")

    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    s = _b64url(sig)

    return f"{h}.{p}.{s}"


def verify_token(token: str, secret: str) -> dict | None:
    """Verify a JWT and return its payload dict, or None if invalid/expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        h_b64, p_b64, s_b64 = parts
        msg = f"{h_b64}.{p_b64}".encode("ascii")

        expected_sig = hmac.new(
            secret.encode("utf-8"), msg, hashlib.sha256
        ).digest()
        actual_sig = _b64url_decode(s_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64url_decode(p_b64))

        # Check expiration
        now = int(time.time())
        if payload.get("exp", 0) < now:
            return None

        return payload

    except Exception:
        return None
