"""One-shot runtime identity generation and loading."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from agent_scheduler.domain.models import utc_now


@dataclass(frozen=True)
class RuntimeIdentity:
    key_id: str
    signing_private_key: Ed25519PrivateKey
    signing_public_key: Ed25519PublicKey
    worker_api_key: str
    tls_certificate: Path
    tls_private_key: Path


def _development_certificate() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(utc_now() - datetime.timedelta(minutes=1))
        .not_valid_after(utc_now() + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
        certificate.public_bytes(serialization.Encoding.PEM),
    )


def init_runtime(state_root: Path) -> dict[str, str]:
    state_root.mkdir(parents=True, exist_ok=True)
    os.chmod(state_root, 0o750)
    for name, mode in (
        ("immutable", 0o700),
        ("events", 0o700),
        ("snapshots", 0o700),
        ("framework-logs", 0o770),
        ("worker-inbox", 0o770),
        ("outputs", 0o770),
        ("qualification", 0o700),
    ):
        directory = state_root / name
        directory.mkdir(exist_ok=True)
        os.chmod(directory, mode)
    secrets_dir = state_root / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    paths = {
        "worker_api_key": secrets_dir / "worker-api-key",
        "key_id": secrets_dir / "ed25519-key-id",
        "ed25519_private": secrets_dir / "ed25519-private.pem",
        "ed25519_public": secrets_dir / "ed25519-public.pem",
        "tls_certificate": secrets_dir / "tls-certificate.pem",
        "tls_private_key": secrets_dir / "tls-private-key.pem",
    }
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("runtime identity already exists; refusing to overwrite")
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_bytes = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_id = f"ed25519-{hashlib.sha256(public_bytes).hexdigest()[:16]}"
    paths["worker_api_key"].write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    paths["key_id"].write_text(key_id + "\n", encoding="ascii")
    paths["ed25519_private"].write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    paths["ed25519_public"].write_bytes(public_bytes)
    tls_key, tls_certificate = _development_certificate()
    paths["tls_private_key"].write_bytes(tls_key)
    paths["tls_certificate"].write_bytes(tls_certificate)
    for path in paths.values():
        os.chmod(path, 0o600)
    return {name: str(path) for name, path in paths.items()}


def load_runtime(state_root: Path) -> RuntimeIdentity:
    secrets_dir = state_root / "secrets"
    private_path = secrets_dir / "ed25519-private.pem"
    public_path = secrets_dir / "ed25519-public.pem"
    key_id_path = secrets_dir / "ed25519-key-id"
    worker_key_path = secrets_dir / "worker-api-key"
    tls_certificate = secrets_dir / "tls-certificate.pem"
    tls_private_key = secrets_dir / "tls-private-key.pem"
    required = (
        private_path,
        public_path,
        key_id_path,
        worker_key_path,
        tls_certificate,
        tls_private_key,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"runtime identity is incomplete: {', '.join(missing)}")
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    public = serialization.load_pem_public_key(public_path.read_bytes())
    if not isinstance(private, Ed25519PrivateKey) or not isinstance(public, Ed25519PublicKey):
        raise TypeError("runtime signing keys must be Ed25519")
    derived = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    configured = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if not secrets.compare_digest(derived, configured):
        raise ValueError("runtime public/private signing keys do not match")
    identity = RuntimeIdentity(
        key_id=key_id_path.read_text(encoding="ascii").strip(),
        signing_private_key=private,
        signing_public_key=public,
        worker_api_key=worker_key_path.read_text(encoding="utf-8").strip(),
        tls_certificate=tls_certificate,
        tls_private_key=tls_private_key,
    )
    validate_runtime(state_root, identity)
    return identity


def validate_runtime(state_root: Path, identity: RuntimeIdentity) -> None:
    required_directories = (
        "immutable",
        "events",
        "snapshots",
        "framework-logs",
        "worker-inbox",
        "outputs",
        "secrets",
    )
    missing = [name for name in required_directories if not (state_root / name).is_dir()]
    if missing:
        raise FileNotFoundError(f"runtime directories are missing: {', '.join(missing)}")
    private_paths = (
        state_root / "secrets" / "worker-api-key",
        state_root / "secrets" / "ed25519-private.pem",
        identity.tls_private_key,
    )
    insecure = [str(path) for path in private_paths if path.stat().st_mode & 0o077]
    if insecure:
        raise PermissionError(f"runtime private files are too permissive: {', '.join(insecure)}")
    if not identity.key_id or len(identity.worker_api_key) < 32:
        raise ValueError("runtime key identity is invalid")
