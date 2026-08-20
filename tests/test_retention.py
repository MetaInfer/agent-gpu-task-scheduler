import os
from pathlib import Path

from agent_scheduler.storage import prune_framework_logs


def test_only_old_framework_logs_are_pruned(tmp_path: Path):
    old = tmp_path / "framework-logs" / "task" / "old.log"
    current = tmp_path / "framework-logs" / "task" / "current.log"
    output = tmp_path / "outputs" / "result.json"
    for path in (old, current, output):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    now = 10_000_000.0
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(current, (now, now))
    os.utime(output, (now - 100 * 86400, now - 100 * 86400))
    assert prune_framework_logs(tmp_path, now=now) == 1
    assert not old.exists()
    assert current.exists()
    assert output.exists()
