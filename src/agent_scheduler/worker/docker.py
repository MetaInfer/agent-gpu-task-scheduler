"""Docker CLI adapter with argv-only execution and inspect-based truth."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass


class DockerError(RuntimeError):
    pass


class DockerTimeout(DockerError):
    pass


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    read_write: bool


@dataclass(frozen=True)
class ContainerInspection:
    exists: bool
    running: bool
    image_id: str | None = None
    config_image: str | None = None
    user: str | None = None
    working_dir: str | None = None
    privileged: bool = False
    network_mode: str | None = None
    ipc_mode: str | None = None
    shm_size: int | None = None
    devices: tuple[tuple[str, str], ...] = ()
    mounts: tuple[Mount, ...] = ()


class DockerCLI:
    def __init__(self, executable: str = "docker") -> None:
        self.executable = executable

    def run(
        self, args: Sequence[str], *, stdin: str | None = None, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *args],
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerTimeout(f"docker command timed out: {args[0] if args else ''}") from exc
        except OSError as exc:
            raise DockerError("docker invocation failed") from exc

    def inspect(self, name: str) -> ContainerInspection:
        result = self.run(["inspect", name])
        if result.returncode != 0:
            if "No such object" in result.stderr:
                return ContainerInspection(False, False)
            raise DockerError(result.stderr.strip() or "docker inspect failed")
        try:
            values = json.loads(result.stdout)
            value = values[0]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise DockerError("docker inspect returned invalid JSON") from exc
        devices = tuple(
            sorted(
                (
                    str(item.get("PathOnHost", "")),
                    str(item.get("PathInContainer", "")),
                )
                for item in value.get("HostConfig", {}).get("Devices", [])
            )
        )
        mounts = tuple(
            sorted(
                (
                    Mount(
                        source=str(item.get("Source", "")),
                        destination=str(item.get("Destination", "")),
                        read_write=bool(item.get("RW", False)),
                    )
                    for item in value.get("Mounts", [])
                ),
                key=lambda item: item.destination,
            )
        )
        host = value.get("HostConfig", {})
        config = value.get("Config", {})
        state = value.get("State", {})
        return ContainerInspection(
            exists=True,
            running=bool(state.get("Running", False)),
            image_id=str(value.get("Image", "")),
            config_image=str(config.get("Image", "")),
            user=str(config.get("User", "")),
            working_dir=str(config.get("WorkingDir", "")),
            privileged=bool(host.get("Privileged", False)),
            network_mode=str(host.get("NetworkMode", "")),
            ipc_mode=str(host.get("IpcMode", "")),
            shm_size=int(host.get("ShmSize", 0)),
            devices=devices,
            mounts=mounts,
        )

    def image_repo_digests(self, image: str) -> tuple[str, ...]:
        result = self.run(["image", "inspect", "--format", "{{json .RepoDigests}}", image])
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or "docker image inspect failed")
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerError("docker image inspect returned invalid JSON") from exc
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise DockerError("docker RepoDigests is not a string list")
        return tuple(values)

    def start(self, name: str) -> None:
        result = self.run(["start", name])
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or "docker start failed")
        if not self.inspect(name).running:
            raise DockerError("container is not running after docker start")

    def stop(self, name: str, grace_seconds: int = 30) -> tuple[ContainerInspection, str | None]:
        stop_error: DockerError | None = None
        result: subprocess.CompletedProcess[str] | None = None
        try:
            result = self.run(
                ["stop", "--time", str(grace_seconds), name],
                timeout=grace_seconds + 15,
            )
        except DockerError as exc:
            stop_error = exc
        status = self.inspect(name)
        warning = None
        if status.running:
            kill_error: DockerError | None = None
            kill: subprocess.CompletedProcess[str] | None = None
            try:
                kill = self.run(["kill", name])
            except DockerError as exc:
                kill_error = exc
            status = self.inspect(name)
            if status.running:
                if kill_error is not None:
                    raise DockerError("container remains running after kill error") from kill_error
                raise DockerError(
                    kill.stderr.strip()
                    if kill and kill.stderr.strip()
                    else "container remains running"
                )
            warning = "DOCKER_STOP_REQUIRED_KILL"
        if stop_error is not None or (result is not None and result.returncode != 0):
            warning = warning or "DOCKER_STOP_NONZERO_POSTCONDITION_MET"
        return status, warning

    def exec_attached(
        self,
        name: str,
        argv: Sequence[str],
        *,
        environment: dict[str, str],
        stdin: str | None = None,
        timeout: float = 600,
    ) -> subprocess.CompletedProcess[str]:
        env_args = [
            item for key, value in sorted(environment.items()) for item in ("-e", f"{key}={value}")
        ]
        return self.run(["exec", "-i", *env_args, name, *argv], stdin=stdin, timeout=timeout)
