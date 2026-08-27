"""Console entrypoint for the client-only Submitter MCP adapter."""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from agent_scheduler_client.mcp import SubmitterMCPAdapter

_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "base URL must be HTTPS with a host and no userinfo, query, or fragment"
        )
    return value.rstrip("/")


def _username(value: str) -> str:
    if not _USERNAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "username must match [A-Za-z0-9_.-]{1,64}"
        )
    return value


def _readable_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise argparse.ArgumentTypeError(f"CA file is not a readable regular file: {path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-scheduler-submitter")
    parser.add_argument("--base-url", required=True, type=_https_url)
    parser.add_argument("--username", required=True, type=_username)
    parser.add_argument("--ca-file", required=True, type=_readable_file)
    return parser


def _adapter_type() -> type[SubmitterMCPAdapter]:
    from agent_scheduler_client.mcp import SubmitterMCPAdapter

    return SubmitterMCPAdapter


def run_mcp(
    *,
    base_url: str,
    username: str,
    ca_file: Path,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    adapter = _adapter_type()(base_url, username, verify=str(ca_file))
    try:
        adapter.run_stdio(input_stream, output_stream)
    finally:
        adapter.close()
    return 0


def _terminate(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    previous: Callable[[int, FrameType | None], object] | int | None = None
    if hasattr(signal, "SIGTERM"):
        previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run_mcp(
            base_url=args.base_url,
            username=args.username,
            ca_file=args.ca_file,
        )
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
