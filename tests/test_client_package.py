from pathlib import Path

from agent_scheduler_client import __version__
from agent_scheduler_client.mcp import SubmitterMCPAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_client_adapter_has_one_source_location():
    assert __version__ == "0.2.0"
    assert SubmitterMCPAdapter.__module__ == "agent_scheduler_client.mcp"
    assert (
        PROJECT_ROOT
        / "packages"
        / "client"
        / "src"
        / "agent_scheduler_client"
        / "mcp.py"
    ).is_file()
    assert not (PROJECT_ROOT / "src" / "agent_scheduler" / "adapters" / "mcp.py").exists()
