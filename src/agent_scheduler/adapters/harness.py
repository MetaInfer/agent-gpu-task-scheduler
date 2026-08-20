"""Harness seam: deterministic fake and opt-in Claude Code adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from agent_scheduler.domain.models import (
    Command,
    CommandKind,
    ProposalFacts,
    Review,
    ReviewDecision,
    Revision,
    new_id,
    utc_now,
)
from agent_scheduler.storage.events import EventStore

T = TypeVar("T", bound=BaseModel)
_LAUNCHER_PATH = "/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh"
_LAUNCHER_SHA256 = "66de599723417262b3d6c2c2e665777c6c2183a770ed1dbce2d69fcb074881f0"
_IMAGE_DIGEST = (
    "harbor.sourcefind.cn:5443/dcu/admin/base/custom@"
    "sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238"
)


class HarnessError(RuntimeError):
    pass


class HarnessAdapter(Protocol):
    def process(self, revision: Revision) -> ProposalFacts: ...

    def review(self, revision: Revision, facts: ProposalFacts) -> Review: ...

    def control(self, assignment_id: str, dispatch_generation: int) -> str: ...


@dataclass
class FakeHarnessAdapter:
    """Stable adapter used by normal tests; it exercises the same contracts."""

    worker_id: str = "worker-local-01"
    container_name: str = "fh-sglang-deepseek-v4-flash"
    submitter_username: str = "zz_chentian"
    container_user: str = "root"
    image_digest: str = _IMAGE_DIGEST
    gpu_count: int = 8
    decisions: list[ReviewDecision] = field(default_factory=list)
    process_calls: int = 0
    review_calls: int = 0

    def process(self, revision: Revision) -> ProposalFacts:
        self.process_calls += 1
        requested = _find_card_count(revision.markdown)
        if requested > self.gpu_count:
            raise HarnessError("requested GPU count exceeds worker capacity")
        host_output = f"/public/share/agent-scheduler-mvp/outputs/{revision.proposal_id}.json"
        host_log = f"/public/share/agent-scheduler-mvp/logs/{revision.proposal_id}.log"
        container_output = host_output.replace("/public/share", "/data", 1)
        container_log = host_log.replace("/public/share", "/data", 1)
        return ProposalFacts(
            facts_id=new_id("facts"),
            revision_id=revision.revision_id,
            worker_count=1,
            gpu_count=self.gpu_count,
            required_gpu_count=requested,
            required_worker_id=self.worker_id,
            container_name=self.container_name,
            submitter_username=self.submitter_username,
            container_user=self.container_user,
            image_digest=self.image_digest,
            run=(
                Command(
                    kind=CommandKind.CONTAINER_PATH_BASH,
                    container_path=_LAUNCHER_PATH,
                    sha256=_LAUNCHER_SHA256,
                    argv=(container_output, container_log),
                ),
            ),
            required_logs=(host_log,),
            required_outputs=(host_output,),
            timeout_seconds=600,
        )

    def review(self, revision: Revision, facts: ProposalFacts) -> Review:
        self.review_calls += 1
        decision = self.decisions.pop(0) if self.decisions else ReviewDecision.APPROVE
        return Review(
            review_id=new_id("review"),
            proposal_id=revision.proposal_id,
            revision_id=revision.revision_id,
            decision=decision,
            rationale=f"Fake reviewer returned {decision.value} for the validated smoke task.",
            created_at=utc_now(),
        )

    def control(self, assignment_id: str, dispatch_generation: int) -> str:
        return f"fake-supervisor:{assignment_id}:{dispatch_generation}"


class ClaudeCodeAdapter:
    """Strict CLI adapter; domain transitions remain in deterministic Python."""

    def __init__(
        self,
        events: EventStore,
        *,
        executable: str = "claude",
        prompts_dir: Path = Path("prompts"),
        mcp_config: Path = Path("config/empty-mcp.json"),
        timeout_seconds: int = 600,
        max_attempts: int = 4,
    ) -> None:
        self.events = events
        self.executable = executable
        self.prompts_dir = prompts_dir.resolve()
        self.mcp_config = mcp_config.resolve()
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def process(self, revision: Revision) -> ProposalFacts:
        facts_id = new_id("facts")
        prompt = (
            "Produce ProposalFacts for this immutable revision. Echo the supplied IDs exactly. "
            "Do not approve the proposal.\n\n"
            f"facts_id={facts_id}\nrevision_id={revision.revision_id}\n"
            f"revision_markdown:\n{revision.markdown}"
        )
        result = self._invoke("processor", prompt, ProposalFacts)
        if result.facts_id != facts_id or result.revision_id != revision.revision_id:
            raise HarnessError("Processor returned identifiers not bound to the invocation")
        return result

    def review(self, revision: Revision, facts: ProposalFacts) -> Review:
        review_id = new_id("review")
        prompt = (
            "Independently review this revision and its validated facts. Return only APPROVE, "
            "REQUEST_CHANGES, or REJECT through the schema. Echo all supplied IDs exactly.\n\n"
            f"review_id={review_id}\nproposal_id={revision.proposal_id}\n"
            f"revision_id={revision.revision_id}\nrevision:\n{revision.markdown}\n\n"
            f"facts:\n{facts.model_dump_json()}"
        )
        result = self._invoke("reviewer", prompt, Review)
        expected = (review_id, revision.proposal_id, revision.revision_id)
        actual = (result.review_id, result.proposal_id, result.revision_id)
        if actual != expected:
            raise HarnessError("Reviewer returned identifiers not bound to the invocation")
        return result

    def control(self, assignment_id: str, dispatch_generation: int) -> str:
        prompt = (
            "Request deterministic Worker Driver ownership transfer for the supplied assignment. "
            "Do not construct commands or recovery actions.\n"
            f"assignment_id={assignment_id}\ndispatch_generation={dispatch_generation}"
        )
        result = self._invoke("worker-controller", prompt, ControllerResult)
        if (
            result.assignment_id != assignment_id
            or result.dispatch_generation != dispatch_generation
        ):
            raise HarnessError("Controller returned identifiers not bound to the invocation")
        return result.supervisor_handle

    def _invoke(self, role: str, prompt: str, model: type[T]) -> T:
        system_prompt_path = self.prompts_dir / f"{role}.md"
        if not system_prompt_path.is_file() or not self.mcp_config.is_file():
            raise HarnessError(f"missing versioned Harness configuration for {role}")
        schema = json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            invocation_id = new_id("harness")
            started_at = utc_now()
            command = [
                self.executable,
                "--bare",
                "--print",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                str(self.mcp_config),
                "--system-prompt-file",
                str(system_prompt_path),
                "--output-format",
                "json",
                "--json-schema",
                schema,
            ]
            audit: dict[str, object] = {
                "schema_version": "v1",
                "invocation_id": invocation_id,
                "role": role,
                "attempt": attempt,
                "started_at": started_at.isoformat(),
                "argv": command,
                "cwd": str(self.events.root),
                "input": prompt,
            }
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    cwd=self.events.root,
                    env=self._minimal_environment(),
                )
                audit.update(
                    {
                        "ended_at": utc_now().isoformat(),
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    }
                )
                if completed.returncode != 0:
                    error = HarnessError(f"Claude Code exited with code {completed.returncode}")
                    retryable = _retryable_stderr(completed.stderr)
                    audit["retryable"] = retryable
                    self.events.write_immutable("harness", invocation_id, audit)
                    if not retryable:
                        raise error
                    last_error = error
                else:
                    structured = _structured_output(completed.stdout)
                    validated = model.model_validate_json(
                        json.dumps(structured, sort_keys=True, separators=(",", ":"))
                    )
                    audit["validated_output"] = validated.model_dump(mode="json")
                    self.events.write_immutable("harness", invocation_id, audit)
                    return validated
            except subprocess.TimeoutExpired as exc:
                last_error = exc
                audit.update(
                    {
                        "ended_at": utc_now().isoformat(),
                        "error": "timeout",
                        "retryable": True,
                    }
                )
                self.events.write_immutable("harness", invocation_id, audit)
            except (json.JSONDecodeError, ValidationError, TypeError, KeyError) as exc:
                last_error = exc
                audit.update(
                    {
                        "ended_at": utc_now().isoformat(),
                        "error": f"invalid structured output: {type(exc).__name__}",
                        "retryable": True,
                    }
                )
                self.events.write_immutable("harness", invocation_id, audit)
            except OSError as exc:
                audit.update(
                    {
                        "ended_at": utc_now().isoformat(),
                        "error": f"process launch failed: {type(exc).__name__}",
                        "retryable": False,
                    }
                )
                self.events.write_immutable("harness", invocation_id, audit)
                raise HarnessError("Claude Code process could not be launched") from exc
            if attempt < self.max_attempts:
                time.sleep(2 ** (attempt - 1))
        raise HarnessError(f"Claude Code failed after {self.max_attempts} attempts") from last_error

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        env = {
            "HOME": os.environ.get("HOME", "/root"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DISABLE_UPDATES": "1",
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
        }
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        ):
            value = os.environ.get(name)
            if value:
                env[name] = value
        return env


class ControllerResult(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    assignment_id: str
    dispatch_generation: int
    supervisor_handle: str


def _structured_output(stdout: str) -> dict[str, object]:
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict):
        raise TypeError("Claude Code JSON output is not an object")
    value = envelope.get("structured_output", envelope.get("result", envelope))
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError("Claude Code structured output is not an object")
    return value


def _retryable_stderr(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in ("rate limit", "429", "timed out", "timeout"))


def _find_card_count(markdown: str) -> int:
    match = re.search(r"(?:gpu|卡数|cards?)\s*[:=：]?\s*(1|2|4|8)\b", markdown, re.IGNORECASE)
    return int(match.group(1)) if match else 1
