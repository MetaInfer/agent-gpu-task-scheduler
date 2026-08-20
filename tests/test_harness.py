import json
import re
import subprocess
from pathlib import Path

from conftest import proposal_markdown

from agent_scheduler.adapters.harness import ClaudeCodeAdapter, FakeHarnessAdapter
from agent_scheduler.domain.models import Revision, new_id, utc_now
from agent_scheduler.storage import EventStore


def revision() -> Revision:
    return Revision(
        revision_id=new_id("rev"),
        proposal_id=new_id("prop"),
        number=1,
        markdown=proposal_markdown(1),
        created_at=utc_now(),
    )


def test_fake_harness_freezes_versioned_launcher_hash():
    item = revision()
    facts = FakeHarnessAdapter().process(item)
    command = facts.run[0]
    launcher = Path("scripts/run_torch_collective_smoke.sh")
    import hashlib

    assert (
        command.container_path
        == "/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh"
    )
    assert command.sha256 == hashlib.sha256(launcher.read_bytes()).hexdigest()
    assert command.argv[0].startswith("/data/agent-scheduler-mvp/outputs/")


def test_claude_adapter_uses_restricted_cli_and_validates_output(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "state")
    item = revision()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        prompt = kwargs["input"]
        facts_id = re.search(r"facts_id=(facts_[0-9a-f]{32})", prompt).group(1)
        revision_id = re.search(r"revision_id=(rev_[0-9a-f]{32})", prompt).group(1)
        facts = (
            FakeHarnessAdapter()
            .process(item)
            .model_copy(update={"facts_id": facts_id, "revision_id": revision_id})
        )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"structured_output": facts.model_dump(mode="json")}),
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = ClaudeCodeAdapter(
        store,
        prompts_dir=Path("prompts"),
        mcp_config=Path("config/empty-mcp.json"),
    )
    facts = adapter.process(item)
    assert facts.revision_id == item.revision_id
    command = captured["command"]
    for required in (
        "--bare",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--tools",
        "--json-schema",
    ):
        assert required in command
    assert item.markdown not in command
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]
    assert len(list(store.iter_immutable("harness"))) == 1
