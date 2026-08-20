"""Framework-log retention; immutable audit and business outputs are untouched."""

from __future__ import annotations

import time
from pathlib import Path


def prune_framework_logs(state_root: Path, *, now: float | None = None, days: int = 30) -> int:
    root = state_root / "framework-logs"
    if not root.exists():
        return 0
    cutoff = (time.time() if now is None else now) - days * 24 * 3600
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return removed
