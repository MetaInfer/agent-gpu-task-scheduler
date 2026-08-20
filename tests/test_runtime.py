import os
from pathlib import Path

import pytest
from cryptography import x509

from agent_scheduler.runtime import init_runtime, load_runtime


def test_runtime_identity_is_real_and_refuses_overwrite(tmp_path: Path):
    root = tmp_path / "state"
    paths = init_runtime(root)
    identity = load_runtime(root)
    assert identity.key_id.startswith("ed25519-")
    x509.load_pem_x509_certificate(Path(paths["tls_certificate"]).read_bytes())
    assert all((os.stat(path).st_mode & 0o777) == 0o600 for path in paths.values())
    with pytest.raises(FileExistsError):
        init_runtime(root)
