"""Submitter MCP stdio Adapter; REST remains the only authority."""

from __future__ import annotations

import json
import sys
from typing import TextIO

import httpx

from agent_scheduler_client import __version__
from agent_scheduler_client.rest import MCPAdapterError, SubmitterRESTClient
from agent_scheduler_client.tools import build_tools


class SubmitterMCPAdapter:
    def __init__(
        self,
        base_url: str,
        username: str,
        verify: bool | str = True,
        rest_client: SubmitterRESTClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.verify = verify
        self._rest = rest_client or SubmitterRESTClient(base_url, username, verify)

    def close(self) -> None:
        self._rest.close()

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self._rest.call_tool(name, arguments)

    def run_stdio(
        self, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout
    ) -> None:
        for line in input_stream:
            if not line.strip():
                continue
            request_id: object = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise MCPAdapterError("JSON-RPC message must be an object")
                request_id = message.get("id")
                method = message.get("method")
                if method == "notifications/initialized":
                    continue
                if method == "initialize":
                    params = message.get("params", {})
                    protocol = (
                        params.get("protocolVersion", "2025-11-25")
                        if isinstance(params, dict)
                        else "2025-11-25"
                    )
                    result: dict[str, object] = {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "agent-scheduler-submitter", "version": __version__},
                    }
                elif method == "tools/list":
                    result = {"tools": build_tools(self.username)}
                elif method == "tools/call":
                    params = message.get("params")
                    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                        raise MCPAdapterError("tools/call requires name and arguments")
                    arguments = params.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise MCPAdapterError("tool arguments must be an object")
                    value = self.call_tool(params["name"], arguments)
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(value, sort_keys=True, ensure_ascii=False),
                            }
                        ],
                        "isError": False,
                    }
                else:
                    raise MCPAdapterError(f"unsupported method: {method}")
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (MCPAdapterError, httpx.HTTPError, json.JSONDecodeError, TypeError) as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()
