"""Canonical JSON, content hashes, and Ed25519 signatures."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from typing import Any, TypeVar

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Encode a JSON value using RFC 8785 canonicalization."""
    return rfc8785.dumps(_json_value(value))


def content_hash(value: BaseModel | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def signed_payload(model: T) -> bytes:
    """Canonicalize an object without its mutable integrity fields."""
    data = model.model_dump(mode="json", exclude={"content_hash", "signature"})
    return canonical_bytes(data)


def sign_model(model: T, private_key: Ed25519PrivateKey) -> T:
    payload = signed_payload(model)
    digest = hashlib.sha256(payload).hexdigest()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    return model.model_copy(update={"content_hash": digest, "signature": signature})


def verify_model(model: BaseModel, public_key: Ed25519PublicKey) -> bool:
    digest = getattr(model, "content_hash", None)
    signature = getattr(model, "signature", None)
    if not isinstance(digest, str) or not isinstance(signature, str):
        return False
    payload = signed_payload(model)
    if hashlib.sha256(payload).hexdigest() != digest:
        return False
    try:
        decoded = base64.b64decode(signature, validate=True)
        if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != signature:
            return False
        public_key.verify(decoded, payload)
    except (binascii.Error, InvalidSignature, ValueError):
        return False
    return True


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()
