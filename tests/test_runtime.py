import os
import stat
from pathlib import Path

import pytest
from cryptography import x509

from agent_scheduler.runtime import init_runtime, load_runtime, load_tls_certificate


def test_runtime_identity_is_real_and_refuses_overwrite(tmp_path: Path):
    root = tmp_path / "state"
    paths = init_runtime(root)
    identity = load_runtime(root)
    assert identity.key_id.startswith("ed25519-")
    x509.load_pem_x509_certificate(Path(paths["tls_certificate"]).read_bytes())
    private_paths = {name: path for name, path in paths.items() if name != "tls_certificate"}
    assert all((os.stat(path).st_mode & 0o777) == 0o600 for path in private_paths.values())
    with pytest.raises(FileExistsError):
        init_runtime(root)


def test_tls_certificate_and_directory_are_group_readable(tmp_path: Path):
    """The certificate is public material; a non-root Submitter must be able to read it
    without any access to `secrets/`. Both the containing directory and the file itself
    carry state_root's own group, so access follows however an admin already provisioned
    the Submitter's OS account."""
    root = tmp_path / "state"
    paths = init_runtime(root)
    certificate = Path(paths["tls_certificate"])
    tls_dir = certificate.parent
    assert certificate == root / "tls" / "certificate.pem"
    assert not (root / "secrets" / "tls-certificate.pem").exists()
    state_root_gid = os.stat(root).st_gid
    assert stat.S_IMODE(os.stat(tls_dir).st_mode) == 0o750
    assert os.stat(tls_dir).st_gid == state_root_gid
    assert stat.S_IMODE(os.stat(certificate).st_mode) == 0o640
    assert os.stat(certificate).st_gid == state_root_gid


def test_load_tls_certificate_never_touches_signing_material(tmp_path: Path):
    """Deleting every other secret must not affect this loader — that is what makes it
    safe for the unprivileged `mcp` CLI command to use instead of `load_runtime`."""
    root = tmp_path / "state"
    init_runtime(root)
    for name in (
        "ed25519-private.pem",
        "ed25519-public.pem",
        "ed25519-key-id",
        "worker-api-key",
    ):
        (root / "secrets" / name).unlink()
    certificate = load_tls_certificate(root)
    assert certificate == root / "tls" / "certificate.pem"
    x509.load_pem_x509_certificate(certificate.read_bytes())


def test_load_tls_certificate_missing_raises(tmp_path: Path):
    root = tmp_path / "state"
    with pytest.raises(FileNotFoundError, match="TLS certificate is missing"):
        load_tls_certificate(root)
