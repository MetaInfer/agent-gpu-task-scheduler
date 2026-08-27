#!/usr/bin/env python3
"""Build a verified Agent Client Kit from explicit release inputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_scheduler.client_kit import KitBuildInputs, build_client_kit


def _parse_harness_versions(
    parser: argparse.ArgumentParser,
    values: list[str],
) -> dict[str, str]:
    if len(values) != 4:
        parser.error("--harness-version must be repeated exactly four times")
    versions: dict[str, str] = {}
    for value in values:
        name, separator, version = value.partition("=")
        if not separator or not name or not version:
            parser.error("--harness-version must use NAME=VERSION")
        if name in versions:
            parser.error(f"duplicate harness name: {name}")
        versions[name] = version
    return versions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--client-wheel", type=Path, required=True)
    parser.add_argument("--dependency-wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kit-version", required=True)
    parser.add_argument("--harness-version", action="append", default=[], metavar="NAME=VERSION")
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    harness_versions = _parse_harness_versions(parser, arguments.harness_version)
    output = build_client_kit(
        KitBuildInputs(
            project_root=arguments.project_root,
            client_wheel=arguments.client_wheel,
            dependency_wheelhouse=arguments.dependency_wheelhouse,
            output_dir=arguments.output_dir,
            kit_version=arguments.kit_version,
            tested_harnesses=harness_versions,
        )
    )
    print(output)


if __name__ == "__main__":
    main()
