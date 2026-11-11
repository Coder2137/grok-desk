"""AES-256-GCM envelopes for the public live files.

The key is the SHA-256 of the operator path. This module never logs that path.
"""

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALG = "AES-256-GCM"
VERSION = 1
_CORE = ("state.json", "chart.json", "activity.json")


def derive_key(data_path: str) -> bytes:
    return hashlib.sha256(data_path.encode("utf-8")).digest()


def is_live_path(relative: str) -> bool:
    if not relative or "\\" in relative or relative.startswith("/"):
        return False
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if relative in _CORE:
        return True
    if len(parts) == 2 and parts[0] == "coin" and _coin_file(parts[1]):
        return True
    if len(parts) == 2 and parts[0] in ("desk2", "desk3") and parts[1] in _CORE:
        return True
    return len(parts) == 3 and parts[0] in ("desk2", "desk3") and parts[1] == "coin" and _coin_file(parts[2])


def _coin_file(name: str) -> bool:
    return name.endswith(".json") and len(name) > len(".json") and name not in (".json",)


def encrypt_envelope(plaintext: bytes, data_path: str, aad_name: str, nonce: bytes | None = None) -> dict:
    if not is_live_path(aad_name):
        raise ValueError("aad")
    if nonce is None:
        nonce = os.urandom(12)
    if len(nonce) != 12:
        raise ValueError("nonce")
    token = AESGCM(derive_key(data_path)).encrypt(nonce, plaintext, aad_name.encode("utf-8"))
    return {
        "v": VERSION,
        "alg": ALG,
        "n": base64.b64encode(nonce).decode("ascii"),
        "c": base64.b64encode(token).decode("ascii"),
    }


def envelope_bytes(envelope: dict) -> bytes:
    payload = {
        "v": envelope["v"],
        "alg": envelope["alg"],
        "n": envelope["n"],
        "c": envelope["c"],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decrypt_envelope(envelope: dict, data_path: str, aad_name: str) -> bytes:
    if not isinstance(envelope, dict) or envelope.get("v") != VERSION or envelope.get("alg") != ALG:
        raise ValueError("envelope")
    if not is_live_path(aad_name):
        raise ValueError("aad")
    nonce = base64.b64decode(envelope["n"], validate=True)
    token = base64.b64decode(envelope["c"], validate=True)
    if len(nonce) != 12:
        raise ValueError("nonce")
    return AESGCM(derive_key(data_path)).decrypt(nonce, token, aad_name.encode("utf-8"))
