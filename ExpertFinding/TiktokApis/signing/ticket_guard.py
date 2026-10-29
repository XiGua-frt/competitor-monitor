"""Pure-Python tt-ticket-guard header construction.

Creator Studio signs the short tuple ``ticket,path,timestamp`` with an ECDSA
P-256 key, then base64-encodes a compact JSON envelope.  The server ticket is
stored by the browser as AES-GCM ciphertext; the key derivation mirrors the
Web SDK's PBKDF2/SHA-256 implementation.
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


_AES_SECRET = b"tt-ticket-guard-iv"
_AES_SALT = b"secure-salt"
_AES_ITERATIONS = 1000


def _b64decode(value: str) -> bytes:
    return base64.b64decode(str(value).encode("ascii"), validate=False)


def decrypt_encrypt_ticket(encrypt_ticket: str) -> str:
    """Decrypt the browser ``encrypt_ticket`` storage value to its ticket."""
    ciphertext = _b64decode(encrypt_ticket)
    if len(ciphertext) <= 12 + 16:
        raise ValueError("encrypt_ticket 长度不足，无法解密")
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=_AES_SALT,
        iterations=_AES_ITERATIONS,
    ).derive(_AES_SECRET)
    plaintext = AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], None)
    ticket = plaintext.decode("utf-8")
    if not ticket:
        raise ValueError("encrypt_ticket 解密为空")
    return ticket


def public_key_base64(private_key_pem: str) -> str:
    """Return Chrome's 65-byte uncompressed P-256 public key as base64."""
    private_key = serialization.load_pem_private_key(
        str(private_key_pem).encode("utf-8"), password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("ticket guard private key 不是 EC 私钥")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.b64encode(public_bytes).decode("ascii")


def sign_ticket_path(private_key_pem: str, ticket: str, path: str,
                     timestamp: int) -> str:
    """Sign the SDK's ``ticket=<...>&path=<...>&timestamp=<...>`` string."""
    private_key = serialization.load_pem_private_key(
        str(private_key_pem).encode("utf-8"), password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("ticket guard private key 不是 EC 私钥")
    sign_data = f"ticket={ticket}&path={path}&timestamp={int(timestamp)}"
    der_signature = private_key.sign(sign_data.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(der_signature).decode("ascii")


def encode_client_data(*, ts_sign: str, req_sign: str, timestamp: int,
                       req_content: str = "ticket,path,timestamp") -> str:
    """Encode the exact compact JSON envelope sent in client-data."""
    payload = {
        "ts_sign": str(ts_sign),
        "req_content": str(req_content),
        "req_sign": str(req_sign),
        "timestamp": int(timestamp),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_client_data(value: str) -> dict[str, object]:
    """Decode and validate a captured/browser client-data envelope."""
    raw = _b64decode(value).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("client-data 不是 JSON 对象")
    for key in ("ts_sign", "req_content", "req_sign", "timestamp"):
        if key not in payload:
            raise ValueError(f"client-data 缺少 {key}")
    return payload


def build_headers(*, private_key_pem: str, encrypt_ticket: str,
                  ts_sign: str, path: str, timestamp: int | None = None,
                  version: str = "2", iteration_version: str = "0") -> dict[str, str]:
    """Build the five browser ticket-guard headers from fresh SDK state."""
    now = int(timestamp if timestamp is not None else time.time())
    ticket = decrypt_encrypt_ticket(encrypt_ticket)
    req_sign = sign_ticket_path(private_key_pem, ticket, path, now)
    client_data = encode_client_data(
        ts_sign=ts_sign, req_sign=req_sign, timestamp=now,
    )
    return {
        "tt-ticket-guard-public-key": public_key_base64(private_key_pem),
        "tt-ticket-guard-web-version": "1",
        "tt-ticket-guard-version": str(version),
        "tt-ticket-guard-iteration-version": str(iteration_version),
        "tt-ticket-guard-client-data": client_data,
    }


__all__ = [
    "build_headers", "decode_client_data", "decrypt_encrypt_ticket",
    "encode_client_data", "public_key_base64", "sign_ticket_path",
]
