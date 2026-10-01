"""RFC 6238 time-based one-time passwords for Google Authenticator style apps.

Only the standard library is used: secrets for key generation, base32 for the
shared secret, and HMAC-SHA1 with dynamic truncation (RFC 4226) for the codes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime, timezone
from urllib.parse import quote

ISSUER = "AkremMobile"
PERIOD_SECONDS = 30
DIGITS = 6
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommended key length.


def generate_secret() -> str:
    """Return a new random 160-bit secret as unpadded base32 (32 characters)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    """Build the ``otpauth://`` URI that authenticator apps read from a QR code."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='@')}"
    return (
        f"otpauth://totp/{label}?secret={_normalize_secret(secret)}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )


def counter_for(at: datetime) -> int:
    """Return the 30-second time-step counter; naive datetimes are treated as UTC."""
    moment = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
    return int(moment.timestamp()) // PERIOD_SECONDS


def totp_code(secret: str, at: datetime) -> str:
    """Return the zero-padded six-digit code for ``secret`` at time ``at``."""
    return _hotp(_decode_secret(secret), counter_for(at))


def matching_counter(
    secret: str,
    code: str,
    at: datetime,
    window: int = 1,
) -> int | None:
    """Return the time-step counter matching ``code`` within ``window`` steps, else None.

    Spaces in the entered code are ignored. Every candidate is compared in
    constant time so timing does not reveal which step (if any) matched.
    """
    if not isinstance(code, str):
        return None
    clean_code = code.replace(" ", "").strip()
    if len(clean_code) != DIGITS or not clean_code.isascii() or not clean_code.isdigit():
        return None
    key = _decode_secret(secret)
    current = counter_for(at)
    matched: int | None = None
    for counter in range(current - window, current + window + 1):
        if counter < 0:
            continue
        if hmac.compare_digest(_hotp(key, counter), clean_code) and matched is None:
            matched = counter
    return matched


def format_secret(secret: str) -> str:
    """Split a secret into space-separated groups of four for manual entry."""
    clean = _normalize_secret(secret)
    return " ".join(clean[index:index + 4] for index in range(0, len(clean), 4))


def _hotp(key: bytes, counter: int) -> str:
    """Compute an RFC 4226 HOTP value with dynamic truncation."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** DIGITS)).zfill(DIGITS)


def _normalize_secret(secret: str) -> str:
    """Uppercase a base32 secret and drop spaces and padding."""
    return secret.replace(" ", "").replace("=", "").upper()


def _decode_secret(secret: str) -> bytes:
    """Decode an unpadded base32 secret, raising ValueError for invalid text."""
    clean = _normalize_secret(secret)
    if not clean:
        raise ValueError("Two-factor secret is empty")
    padded = clean + "=" * (-len(clean) % 8)
    try:
        return base64.b32decode(padded)
    except (ValueError, TypeError) as error:
        raise ValueError("Two-factor secret is not valid base32") from error
