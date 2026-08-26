# 多 Agent Submitter 接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Codex CLI、pi、dsh 与 Claude ​Code 一样能作为 Proposal 发起者接入调度器，并各自产出一套可被 `verify_qualification()` 独立校验的 1/2/4/8 完整证据包。

**Architecture:** 服务端不为任何 agent 分支——四份接入配置最终都指向同一条 `agent-scheduler mcp` 命令，知识包统一收敛到 `.agents/skills/`（Agent Skills 标准）。配置生成逻辑集中在一个模块里，接入文档与资格夹具共用，避免文档与实现漂移。资格夹具的结果不向 Agent 索取，而是跑完后按 `Qualification Run: <run_id>` 从事件存储反推——这样四个 CLI 在结构化输出上的能力差异不会变成正确性要求。

**Tech Stack:** Python 3.10+（开发环境 3.12）、pydantic v2、httpx、pytest、ruff、mypy strict、uv

**Spec:** `docs/superpowers/specs/2026-08-26-multi-agent-submitter-design.md`

## Global Constraints

- Python `>=3.10`；类型注解须在 3.10 下可用（模块已有 `from __future__ import annotations`）
- ruff `line-length = 100`
- mypy `strict = true`，`packages = ["agent_scheduler"]`——`src/` 下新增代码必须完整标注
- 不新增运行时依赖；生成 YAML 用手写字符串，不引入 `pyyaml`
- MCP 工具面维持 **12** 个，不新增工具
- 绝不修改用户 dotfile：`~/.codex/config.toml`、`~/.pi/agent/settings.json`、`$DSH_HOME/profiles/*/cordis.patch.yml`
- 每轮运行配置一律生成到 `<state-root>/qualification/<run_id>/`
- Skill frontmatter 只含 `name` 与 `description` 两个字段
- `prompts/submitter.md` 折进 prompt 正文，不使用任何 CLI 的 system-prompt flag
- 四个 harness 标识固定为 `claude` / `codex` / `pi` / `dsh`
- 默认门禁必须保持通过：`uv run pytest`、`uv run ruff check .`、`uv run mypy src`

---

### Task 1: Skill 搬到 `.agents/skills/`

把知识包搬到四家都能发现的规范位置，Claude ​Code 用 symlink 指回去。

**Files:**
- Create: `.agents/skills/submit-gpu-task/SKILL.md`（由 `git mv` 产生）
- Create: `.agents/skills/submit-gpu-task/reference/proposal-template.md`（由 `git mv` 产生）
- Create: `.claude/skills/submit-gpu-task`（symlink）
- Create: `src/agent_scheduler/adapters/onboarding.py`
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: 无
- Produces: `agent_scheduler.adapters.onboarding.SkillFrontmatter`（frozen dataclass，字段 `name: str`、`description: str`）、`agent_scheduler.adapters.onboarding.read_skill_frontmatter(path: Path) -> SkillFrontmatter`、`agent_scheduler.adapters.onboarding.CANONICAL_SKILL_DIR: str`（值 `".agents/skills/submit-gpu-task"`）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_onboarding.py`：

```python
from __future__ import annotations

from pathlib import Path

from agent_scheduler.adapters.onboarding import (
    CANONICAL_SKILL_DIR,
    read_skill_frontmatter,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_skill_has_valid_frontmatter():
    skill = PROJECT_ROOT / CANONICAL_SKILL_DIR / "SKILL.md"
    frontmatter = read_skill_frontmatter(skill)
    # The Agent Skills standard requires name to match the parent directory.
    # pi relaxes this, codex and Claude Code do not, so hold the strict rule.
    assert frontmatter.name == skill.parent.name
    assert frontmatter.description.strip()


def test_canonical_skill_ships_the_proposal_template():
    template = PROJECT_ROOT / CANONICAL_SKILL_DIR / "reference" / "proposal-template.md"
    assert template.is_file()


def test_claude_skill_directory_points_at_the_canonical_skill():
    claude_skill = PROJECT_ROOT / ".claude" / "skills" / "submit-gpu-task"
    canonical = PROJECT_ROOT / CANONICAL_SKILL_DIR
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == canonical.resolve()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agent_scheduler.adapters.onboarding'`

- [ ] **Step 3: 搬迁 skill 并建立 symlink**

```bash
mkdir -p .agents/skills
git mv .claude/skills/submit-gpu-task .agents/skills/submit-gpu-task
ln -s ../../.agents/skills/submit-gpu-task .claude/skills/submit-gpu-task
git add .claude/skills/submit-gpu-task
```

`.claude/skills/` 下不再有其他内容时该目录仍需保留（symlink 就在其中），无需额外处理。

- [ ] **Step 4: 写最小实现**

创建 `src/agent_scheduler/adapters/onboarding.py`：

```python
"""Single source of truth for how each Agent harness reaches the Submitter MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CANONICAL_SKILL_DIR = ".agents/skills/submit-gpu-task"


class OnboardingError(ValueError):
    pass


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str


def read_skill_frontmatter(path: Path) -> SkillFrontmatter:
    """Parse the leading `---` block of a SKILL.md.

    Deliberately hand-rolled: the skill contract is two flat string keys, and the
    project ships no YAML dependency.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise OnboardingError(f"skill has no frontmatter block: {path}")
    _, _, remainder = text.partition("---\n")
    block, separator, _ = remainder.partition("\n---\n")
    if not separator:
        raise OnboardingError(f"skill frontmatter block is unterminated: {path}")
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, delimiter, value = line.partition(":")
        if not delimiter:
            raise OnboardingError(f"skill frontmatter line is not a key/value pair: {line!r}")
        fields[key.strip()] = value.strip()
    missing = {"name", "description"} - fields.keys()
    if missing:
        raise OnboardingError(f"skill frontmatter is missing {sorted(missing)}: {path}")
    return SkillFrontmatter(name=fields["name"], description=fields["description"])
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: 3 passed

- [ ] **Step 6: 确认默认门禁未被破坏**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add .agents .claude/skills src/agent_scheduler/adapters/onboarding.py tests/test_onboarding.py
git commit -m "refactor: move the submit-gpu-task skill to the tool-agnostic .agents location

Codex and pi both discover .agents/skills natively; Claude Code does not,
so its own skills directory now points at the canonical copy instead of
holding a second one that would drift."
```

---

### Task 2: 让 `422 INVALID_PROPOSAL` 说清期望形态

Submitter 收到的 `422` 实际来自 `_validate_facts()`。它已能定位到哪一项不合，但不说期望什么，接入方只能猜。

**Files:**
- Modify: `src/agent_scheduler/proposal/service.py:743-772`（`_validate_facts`）
- Test: `tests/test_proposal.py`

**Interfaces:**
- Consumes: 无
- Produces: 无新符号；`ProposalError` 的 `message` 内容变得可执行

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_proposal.py` 末尾：

```python
def test_frozen_launcher_rejection_states_the_expected_argv_shape(tmp_path: Path):
    """The 422 message is the only teaching channel once a Proposal is already in flight."""
    from agent_scheduler.domain.models import Command, CommandKind, ProposalFacts, new_id
    from agent_scheduler.proposal.service import ProposalError, ProposalService

    proposal_id = new_id("prop")
    facts = ProposalFacts(
        facts_id=new_id("facts"),
        revision_id=new_id("rev"),
        worker_count=1,
        gpu_count=8,
        required_gpu_count=1,
        required_worker_id="worker-local-01",
        container_name="fh-sglang-deepseek-v4-flash",
        submitter_username="zz_chentian",
        container_user="root",
        image_digest="harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:" + "0" * 64,
        run=(
            Command(
                kind=CommandKind.CONTAINER_PATH_BASH,
                container_path="/data/fh/agent-gpu-task-scheduler/scripts/"
                "run_torch_collective_smoke.sh",
                sha256="c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c",
                argv=("--nproc-per-node", "1", "--output", "/data/out.json"),
            ),
        ),
        required_logs=("/public/share/agent-scheduler-mvp/logs/x.log",),
        required_outputs=("/public/share/agent-scheduler-mvp/outputs/x.json",),
        timeout_seconds=600,
    )
    proposal = _minimal_proposal(proposal_id)

    with pytest.raises(ProposalError) as caught:
        ProposalService._validate_facts(proposal, facts)

    message = str(caught.value)
    assert "exactly two positional arguments" in message
    assert "<container-output-path> <container-log-path>" in message
    assert "4" in message  # the count actually supplied


def test_artifact_mismatch_rejection_states_the_expected_host_paths():
    from agent_scheduler.proposal.service import ProposalError, ProposalService

    proposal_id = new_id("prop")
    output = f"/data/agent-scheduler-mvp/outputs/{proposal_id}.json"
    business_log = f"/data/agent-scheduler-mvp/logs/{proposal_id}.log"
    facts = _frozen_facts(proposal_id, output, business_log).model_copy(
        update={"required_outputs": ("/public/share/wrong.json",)}
    )

    with pytest.raises(ProposalError) as caught:
        ProposalService._validate_facts(_minimal_proposal(proposal_id), facts)

    message = str(caught.value)
    assert "/public/share/agent-scheduler-mvp/outputs/" in message
    assert "bind mount" in message
```

同时在 `tests/test_proposal.py` 顶部附近加两个辅助函数（若已有同名则复用）：

```python
def _minimal_proposal(proposal_id: str) -> Proposal:
    now = utc_now()
    return Proposal(
        proposal_id=proposal_id,
        username="zz_chentian",
        state=ProposalState.CLARIFYING,
        processor_rounds=1,
        review_count=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=7),
        submitter_deadline=None,
    )


def _frozen_facts(proposal_id: str, output: str, business_log: str) -> ProposalFacts:
    return ProposalFacts(
        facts_id=new_id("facts"),
        revision_id=new_id("rev"),
        worker_count=1,
        gpu_count=8,
        required_gpu_count=1,
        required_worker_id="worker-local-01",
        container_name="fh-sglang-deepseek-v4-flash",
        submitter_username="zz_chentian",
        container_user="root",
        image_digest="harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:" + "0" * 64,
        run=(
            Command(
                kind=CommandKind.CONTAINER_PATH_BASH,
                container_path="/data/fh/agent-gpu-task-scheduler/scripts/"
                "run_torch_collective_smoke.sh",
                sha256="c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c",
                argv=(output, business_log),
            ),
        ),
        required_logs=(business_log.replace("/data", "/public/share", 1),),
        required_outputs=(output.replace("/data", "/public/share", 1),),
        timeout_seconds=600,
    )
```

补齐 `tests/test_proposal.py` 的导入：`from datetime import timedelta`，以及 `Command`、`CommandKind`、`Proposal`、`ProposalFacts`、`ProposalState`、`new_id`、`utc_now`（按文件现有导入风格合并）。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_proposal.py -k "states_the_expected" -v`
Expected: FAIL，断言不成立——当前消息是 `qualification run command does not match frozen launcher`，不含期望形态

- [ ] **Step 3: 改写消息**

在 `src/agent_scheduler/proposal/service.py` 的 `_validate_facts` 中，把冻结 launcher 那一段拆开，让每种不匹配各自说清期望。替换现有的：

```python
        command = facts.run[0]
        if (
            command.kind is not CommandKind.CONTAINER_PATH_BASH
            or command.container_path != _QUALIFICATION_LAUNCHER
            or command.sha256 != _QUALIFICATION_LAUNCHER_SHA256
            or len(command.argv) != 2
        ):
            raise ProposalError("qualification run command does not match frozen launcher")
```

为：

```python
        command = facts.run[0]
        if command.kind is not CommandKind.CONTAINER_PATH_BASH:
            raise ProposalError(
                "qualification run command must be container_path_bash, not "
                f"{command.kind.value}"
            )
        if command.container_path != _QUALIFICATION_LAUNCHER:
            raise ProposalError(
                "qualification run command must invoke the frozen launcher "
                f"{_QUALIFICATION_LAUNCHER}, not {command.container_path}"
            )
        if command.sha256 != _QUALIFICATION_LAUNCHER_SHA256:
            raise ProposalError(
                "qualification launcher SHA-256 must be "
                f"{_QUALIFICATION_LAUNCHER_SHA256}, not {command.sha256}"
            )
        if len(command.argv) != 2:
            raise ProposalError(
                "the frozen launcher takes exactly two positional arguments "
                "`<container-output-path> <container-log-path>` and accepts no flags; "
                f"got {len(command.argv)} arguments: {list(command.argv)}"
            )
```

再把产物不匹配那一段替换为带期望值的版本：

```python
        expected_host_output = output.replace("/data", "/public/share", 1)
        expected_host_log = business_log.replace("/data", "/public/share", 1)
        if facts.required_outputs != (expected_host_output,) or facts.required_logs != (
            expected_host_log,
        ):
            raise ProposalError(
                "required host artifacts must be the launcher argv remapped through the "
                f"`/data` -> `/public/share` bind mount: outputs must be "
                f"({expected_host_output!r},) and logs must be ({expected_host_log!r},); "
                f"got outputs {list(facts.required_outputs)} and logs "
                f"{list(facts.required_logs)}"
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_proposal.py -v`
Expected: 全部通过（含新增两条）

- [ ] **Step 5: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/agent_scheduler/proposal/service.py tests/test_proposal.py
git commit -m "fix: tell the Submitter what the frozen launcher contract expects

The 422 said which check failed but not what would pass, so an Agent that
gets the argv shape wrong has nothing to correct toward and guesses again.
Name the expected form and echo what was actually supplied."
```

---

### Task 3: 四家接入配置的生成器

配置生成集中一处，接入文档与资格夹具共用，杜绝文档与实现漂移。

**Files:**
- Modify: `src/agent_scheduler/adapters/onboarding.py`
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: Task 1 的 `onboarding.py`
- Produces:
  - `HARNESSES: tuple[str, ...]` = `("claude", "codex", "pi", "dsh")`
  - `OnboardingConfig`（frozen dataclass）：`harness: str`、`files: dict[Path, str]`、`argv: tuple[str, ...]`、`env: dict[str, str]`
  - `build_onboarding(harness: str, *, output_dir: Path, project_root: Path, state_root: Path, base_url: str, username: str, uv_path: Path) -> OnboardingConfig`
  - `write_onboarding(config: OnboardingConfig) -> None`

- [ ] **Step 1: 先取到 dsh-mcp-bridge 的真实配置 schema**

生成器不能凭空写 dsh 的 patch 结构。先安装并读出真实 schema：

```bash
dsh plugin --profile headless add dsh-mcp-bridge
dsh --profile headless --dump-config | grep -A 20 -i mcp
```

记下 bridge 插件的 `id`、`name` 与 `config` 字段形态，Step 4 的 `_dsh_patch()` 按实测结构写。若 `dsh plugin add` 失败（网络或 registry 不可达），停下并向用户报告——这是 dsh 接入的硬前置，不要猜结构继续。

- [ ] **Step 2: 写失败的测试**

追加到 `tests/test_onboarding.py`：

```python
import json

import pytest

from agent_scheduler.adapters.onboarding import (
    HARNESSES,
    OnboardingError,
    build_onboarding,
    write_onboarding,
)


def _build(harness: str, tmp_path: Path):
    return build_onboarding(
        harness,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=Path("/public/share/agent-scheduler-mvp"),
        base_url="https://127.0.0.1:8443",
        username="zz_chentian",
        uv_path=Path("/usr/local/bin/uv"),
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_harness_reaches_the_same_mcp_command(harness: str, tmp_path: Path):
    """No Agent gets a bespoke server; the onboarding differs, the target does not."""
    config = _build(harness, tmp_path)
    rendered = json.dumps(
        {
            "argv": list(config.argv),
            "env": config.env,
            "files": {path.name: content for path, content in config.files.items()},
        }
    )
    assert "agent-scheduler" in rendered
    assert "/usr/local/bin/uv" in rendered
    assert "https://127.0.0.1:8443" in rendered
    assert "zz_chentian" in rendered
    assert "/public/share/agent-scheduler-mvp" in rendered


@pytest.mark.parametrize("harness", HARNESSES)
def test_generated_files_land_only_under_the_output_directory(harness: str, tmp_path: Path):
    """Per-run config must never touch a user dotfile."""
    output_dir = tmp_path / "run"
    config = _build(harness, tmp_path)
    for path in config.files:
        assert path.parent == output_dir


def test_claude_config_is_a_valid_strict_mcp_config(tmp_path: Path):
    config = _build("claude", tmp_path)
    write_onboarding(config)
    assert "--strict-mcp-config" in config.argv
    index = config.argv.index("--mcp-config")
    written = Path(config.argv[index + 1])
    server = json.loads(written.read_text(encoding="utf-8"))["mcpServers"]["submitter"]
    assert server["command"] == "/usr/local/bin/uv"
    assert server["args"][:3] == ["run", "agent-scheduler", "mcp"]
    assert server["env"]["AGENT_SCHEDULER_STATE_ROOT"] == "/public/share/agent-scheduler-mvp"


def test_pi_reuses_the_same_mcp_json_shape_and_promotes_direct_tools(tmp_path: Path):
    config = _build("pi", tmp_path)
    written = next(path for path in config.files if path.name == "mcp.json")
    server = json.loads(config.files[written])["mcpServers"]["submitter"]
    # Without directTools the 12 tools hide behind pi-mcp-adapter's proxy tool.
    assert set(server["directTools"]) == {
        "create_proposal",
        "reply",
        "confirm_revision",
        "get_reviews",
        "resume",
        "cancel",
        "get_proposal",
        "get_task",
        "cancel_task",
        "wait_for_task",
        "wait_for_events",
        "get_logs",
    }
    assert config.env["PI_CODING_AGENT_DIR"] == str(written.parent)


def test_codex_config_is_passed_as_flags_and_writes_no_file(tmp_path: Path):
    config = _build("codex", tmp_path)
    assert config.files == {}
    assert config.argv.count("-c") == 4
    joined = " ".join(config.argv)
    assert "mcp_servers.submitter.command=/usr/local/bin/uv" in joined


def test_unknown_harness_is_rejected(tmp_path: Path):
    with pytest.raises(OnboardingError):
        _build("gemini", tmp_path)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: FAIL，`ImportError: cannot import name 'HARNESSES'`

- [ ] **Step 4: 写实现**

追加到 `src/agent_scheduler/adapters/onboarding.py`：

```python
import json

HARNESSES = ("claude", "codex", "pi", "dsh")

SUBMITTER_TOOLS = (
    "create_proposal",
    "reply",
    "confirm_revision",
    "get_reviews",
    "resume",
    "cancel",
    "get_proposal",
    "get_task",
    "cancel_task",
    "wait_for_task",
    "wait_for_events",
    "get_logs",
)


@dataclass(frozen=True)
class OnboardingConfig:
    harness: str
    files: dict[Path, str]
    argv: tuple[str, ...]
    env: dict[str, str]


def build_onboarding(
    harness: str,
    *,
    output_dir: Path,
    project_root: Path,
    state_root: Path,
    base_url: str,
    username: str,
    uv_path: Path,
) -> OnboardingConfig:
    """Describe how one harness is pointed at the Submitter MCP server.

    Every harness ends up invoking the same command; only the declaration
    mechanism differs. Nothing here writes outside ``output_dir``.
    """
    if harness not in HARNESSES:
        raise OnboardingError(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
    args = [
        "run",
        "agent-scheduler",
        "mcp",
        "--base-url",
        base_url,
        "--username",
        username,
    ]
    env = {"AGENT_SCHEDULER_STATE_ROOT": str(state_root)}
    server: dict[str, object] = {
        "command": str(uv_path),
        "args": args,
        "cwd": str(project_root),
        "env": env,
    }
    if harness == "claude":
        path = output_dir / "mcp.json"
        return OnboardingConfig(
            harness=harness,
            files={path: _mcp_json(server)},
            argv=("--strict-mcp-config", "--mcp-config", str(path)),
            env={},
        )
    if harness == "pi":
        path = output_dir / "mcp.json"
        promoted = {**server, "directTools": list(SUBMITTER_TOOLS)}
        return OnboardingConfig(
            harness=harness,
            files={path: _mcp_json(promoted)},
            argv=(),
            env={"PI_CODING_AGENT_DIR": str(output_dir)},
        )
    if harness == "codex":
        return OnboardingConfig(
            harness=harness,
            files={},
            argv=(
                "-c",
                f"mcp_servers.submitter.command={uv_path}",
                "-c",
                f"mcp_servers.submitter.args={json.dumps(args)}",
                "-c",
                f'mcp_servers.submitter.cwd="{project_root}"',
                "-c",
                "mcp_servers.submitter.env="
                f'{{AGENT_SCHEDULER_STATE_ROOT="{state_root}"}}',
            ),
            env={},
        )
    path = output_dir / "submitter-mcp.patch.yml"
    return OnboardingConfig(
        harness=harness,
        files={path: _dsh_patch(server, project_root)},
        argv=("--patch", str(path)),
        env={},
    )


def write_onboarding(config: OnboardingConfig) -> None:
    for path, content in config.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _mcp_json(server: dict[str, object]) -> str:
    return json.dumps({"mcpServers": {"submitter": server}}, sort_keys=True, indent=2) + "\n"


def _dsh_patch(server: dict[str, object], project_root: Path) -> str:
    """Render a cordis patch overlay declaring the submitter server and the skill root.

    The entry shape (`id` / `name` / `config`) is the confirmed cordis patch format;
    the `config` keys below must match what Step 1 observed for the installed bridge.
    """
    args = server["args"]
    assert isinstance(args, list)
    env = server["env"]
    assert isinstance(env, dict)
    rendered_args = ", ".join(_yaml_double_quoted(str(item)) for item in args)
    rendered_env = "\n".join(
        f"            {key}: {_yaml_double_quoted(str(value))}" for key, value in env.items()
    )
    return (
        "# Generated per qualification run. Do not edit; regenerate instead.\n"
        "- id: mcp-bridge\n"
        "  name: dsh-mcp-bridge\n"
        "  config:\n"
        "    servers:\n"
        "      submitter:\n"
        f"        command: {_yaml_double_quoted(str(server['command']))}\n"
        f"        args: [{rendered_args}]\n"
        f"        cwd: {_yaml_double_quoted(str(server['cwd']))}\n"
        "        env:\n"
        f"{rendered_env}\n"
        "- id: skill-filesystem\n"
        "  name: '@deepseek-ai/dsh-skill-filesystem'\n"
        "  config:\n"
        "    roots:\n"
        f"      - {_yaml_double_quoted(str(project_root / '.agents' / 'skills'))}\n"
    )


def _yaml_double_quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
```

Step 1 实测到的 bridge 插件 `id`、`name` 与 `config` 键名若与上面不同，以实测为准修改这三处字面量——`_yaml_double_quoted` 与整体结构不变。同样，`dsh-skill-filesystem` 的 roots 键名以 `--dump-config` 输出为准。

`OnboardingConfig.files` 与 `env` 是可变 dict，`@dataclass(frozen=True)` 只冻结绑定不冻结内容；这与用法一致（构造后即写盘），无需改成 `Mapping`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: 全部通过

- [ ] **Step 6: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add src/agent_scheduler/adapters/onboarding.py tests/test_onboarding.py
git commit -m "feat: generate per-harness onboarding config from one place

Four Agents declare MCP servers four different ways but must all reach the
same command. Deriving every declaration from one builder keeps the docs and
the qualification fixture from drifting apart, and keeps every generated file
inside the per-run directory instead of a user dotfile."
```

---

### Task 4: Submitter harness seam（夹具）

四个 harness 的 argv 与 env 构造。这是测试夹具，不是生产抽象。

**Files:**
- Create: `src/agent_scheduler/adapters/submitter.py`
- Test: `tests/test_submitter_harness.py`

**Interfaces:**
- Consumes: Task 3 的 `build_onboarding`、`write_onboarding`、`HARNESSES`、`OnboardingConfig`
- Produces:
  - `SubmitterInvocation`（frozen dataclass）：`argv: tuple[str, ...]`、`env: dict[str, str]`、`prompt: str`
  - `build_submitter_invocation(harness: str, *, output_dir: Path, project_root: Path, state_root: Path, base_url: str, username: str, uv_path: Path, tls_certificate: Path, executable: str | None = None, run_id: str) -> SubmitterInvocation`
  - `submitter_executable(harness: str) -> str`（默认可执行名：`claude` / `codex` / `pi` / `dsh`）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_submitter_harness.py`：

```python
from __future__ import annotations

from pathlib import Path

import pytest

from agent_scheduler.adapters.onboarding import HARNESSES
from agent_scheduler.adapters.submitter import build_submitter_invocation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _invoke(harness: str, tmp_path: Path):
    return build_submitter_invocation(
        harness,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=Path("/public/share/agent-scheduler-mvp"),
        base_url="https://127.0.0.1:8443",
        username="zz_chentian",
        uv_path=Path("/usr/local/bin/uv"),
        tls_certificate=Path("/public/share/agent-scheduler-mvp/tls/certificate.pem"),
        run_id="qual_abc123",
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_prompt_carries_the_system_prompt_and_the_run_binding(harness: str, tmp_path: Path):
    """codex and dsh have no system-prompt flag, so every harness folds it into the body."""
    invocation = _invoke(harness, tmp_path)
    submitter_prompt = (PROJECT_ROOT / "prompts" / "submitter.md").read_text(encoding="utf-8")
    assert submitter_prompt.strip() in invocation.prompt
    assert "Qualification Run: qual_abc123" in invocation.prompt


@pytest.mark.parametrize("harness", HARNESSES)
def test_invocation_starts_with_the_harness_executable(harness: str, tmp_path: Path):
    invocation = _invoke(harness, tmp_path)
    assert invocation.argv[0] == {"claude": "claude", "codex": "codex", "pi": "pi", "dsh": "dsh"}[
        harness
    ]


@pytest.mark.parametrize("harness", HARNESSES)
def test_tls_certificate_is_exported_so_the_adapter_can_verify_the_master(
    harness: str, tmp_path: Path
):
    invocation = _invoke(harness, tmp_path)
    assert invocation.env["SSL_CERT_FILE"].endswith("tls/certificate.pem")


def test_codex_runs_non_interactively_without_touching_the_user_config(tmp_path: Path):
    invocation = _invoke("codex", tmp_path)
    assert invocation.argv[1] == "exec"
    assert "--json" in invocation.argv
    assert "--skip-git-repo-check" in invocation.argv


def test_dsh_disables_the_approval_prompt(tmp_path: Path):
    """headless defaults to approval: ask, which deadlocks a non-interactive run."""
    invocation = _invoke("dsh", tmp_path)
    assert invocation.env["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert "--profile" in invocation.argv and "headless" in invocation.argv


def test_pi_runs_non_interactively(tmp_path: Path):
    invocation = _invoke("pi", tmp_path)
    assert "--print" in invocation.argv
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_submitter_harness.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agent_scheduler.adapters.submitter'`

- [ ] **Step 3: 写实现**

创建 `src/agent_scheduler/adapters/submitter.py`：

```python
"""Test fixture: launch each Agent harness as a Submitter.

This is not a production abstraction. Submitter Agents are outside our control;
this module exists only so the qualification run can prove the onboarding surface
actually works from all four harnesses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_scheduler.adapters.onboarding import (
    HARNESSES,
    OnboardingError,
    build_onboarding,
    write_onboarding,
)

_EXECUTABLES = {"claude": "claude", "codex": "codex", "pi": "pi", "dsh": "dsh"}


@dataclass(frozen=True)
class SubmitterInvocation:
    argv: tuple[str, ...]
    env: dict[str, str]
    prompt: str


def submitter_executable(harness: str) -> str:
    try:
        return _EXECUTABLES[harness]
    except KeyError as exc:
        raise OnboardingError(f"unknown harness {harness!r}") from exc


def build_submitter_invocation(
    harness: str,
    *,
    output_dir: Path,
    project_root: Path,
    state_root: Path,
    base_url: str,
    username: str,
    uv_path: Path,
    tls_certificate: Path,
    executable: str | None = None,
    run_id: str,
) -> SubmitterInvocation:
    if harness not in HARNESSES:
        raise OnboardingError(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
    onboarding = build_onboarding(
        harness,
        output_dir=output_dir,
        project_root=project_root,
        state_root=state_root,
        base_url=base_url,
        username=username,
        uv_path=uv_path,
    )
    write_onboarding(onboarding)
    binary = executable or submitter_executable(harness)
    prompt = _prompt(project_root, run_id)
    env = _base_environment(tls_certificate)
    env.update(onboarding.env)
    if harness == "claude":
        argv = (
            binary,
            "--print",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--permission-mode",
            "dontAsk",
            *onboarding.argv,
            "--output-format",
            "stream-json",
            "--verbose",
        )
    elif harness == "codex":
        argv = (
            binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(project_root),
            *onboarding.argv,
        )
    elif harness == "pi":
        argv = (binary, "--print", "--mode", "json", *onboarding.argv)
    else:
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
        argv = (binary, "--profile", "headless", *onboarding.argv)
    return SubmitterInvocation(argv=argv, env=env, prompt=prompt)


def _prompt(project_root: Path, run_id: str) -> str:
    """Fold the system prompt into the body; codex and dsh have no flag for it."""
    system_prompt = (project_root / "prompts" / "submitter.md").read_text(encoding="utf-8")
    return (
        f"{system_prompt.strip()}\n\n"
        f"qualification_run_id={run_id}\n"
        "Run the four-task qualification: one Proposal each for 1, 2, 4, and 8 cards. "
        f"Every Proposal you create MUST include the exact line `Qualification Run: {run_id}`. "
        "Drive each Task to a terminal state before reporting."
    )


def _base_environment(tls_certificate: Path) -> dict[str, str]:
    env = {
        "HOME": os.environ.get("HOME", "/root"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DISABLE_UPDATES": "1",
        "SSL_CERT_FILE": str(tls_certificate),
    }
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DEEPSEEK_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    ):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_submitter_harness.py -v`
Expected: 全部通过

- [ ] **Step 5: 处理 pi 的凭据隔离**

`PI_CODING_AGENT_DIR` 改指后，pi 会同时在该目录找 `auth.json`，凭据丢失。写一个测试再实现：

```python
def test_pi_invocation_carries_credentials_into_the_isolated_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "home" / ".pi" / "agent"
    source.mkdir(parents=True)
    (source / "auth.json").write_text('{"providers": {}}', encoding="utf-8")
    (source / "models-store.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    invocation = _invoke("pi", tmp_path)

    isolated = Path(invocation.env["PI_CODING_AGENT_DIR"])
    assert (isolated / "auth.json").is_file()
    assert (isolated / "models-store.json").is_file()
```

在 `build_submitter_invocation` 的 pi 分支前加入：

```python
    if harness == "pi":
        _seed_pi_agent_dir(output_dir)
```

并实现：

```python
def _seed_pi_agent_dir(output_dir: Path) -> None:
    """Copy credentials into the isolated agent dir.

    pi resolves auth.json from PI_CODING_AGENT_DIR, so redirecting that variable for
    per-run isolation would otherwise drop the provider credentials with it.
    """
    source = Path(os.environ.get("HOME", "/root")) / ".pi" / "agent"
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "models-store.json"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, output_dir / name)
```

在模块顶部加 `import shutil`。

Run: `uv run pytest tests/test_submitter_harness.py -v`
Expected: 全部通过

- [ ] **Step 6: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add src/agent_scheduler/adapters/submitter.py tests/test_submitter_harness.py
git commit -m "feat: build Submitter invocations for all four Agent harnesses

A test fixture, not a production seam: Submitter Agents are outside our
control, so this exists only to prove the onboarding surface works from
every harness. Folding the system prompt into the body removes the one
axis where codex and dsh have no equivalent flag."
```

---

### Task 5: 驱动侧重建资格结果

不向 Agent 索取结果——按 `Qualification Run: <run_id>` 从事件存储反推。

**Files:**
- Modify: `src/agent_scheduler/qualification.py`
- Test: `tests/test_qualification.py`

**Interfaces:**
- Consumes: 现有 `QualificationItem`、`QualificationResult`、`EventStore`、`_latest_task_status`
- Produces: `reconstruct_qualification_result(store: EventStore, run_id: str) -> QualificationResult`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_qualification.py`：

```python
from conftest import proposal_markdown, signed_task

from agent_scheduler.domain.models import (
    Revision,
    TaskState,
    TaskStatus,
    new_id,
    utc_now,
)
from agent_scheduler.storage import EventStore


def _seed_qualification_item(
    store: EventStore,
    identity,
    run_id: str,
    cards: int,
    state: str = "COMPLETED",
) -> None:
    """Write the minimum Ground Truth the reconstruction reads: a revision bound to
    the run, a COMPILED Proposal snapshot, the Task, and its latest status."""
    task = signed_task(identity, cards)
    revision = Revision(
        revision_id=task.revision_id,
        proposal_id=task.proposal_id,
        number=1,
        markdown=f"{proposal_markdown(cards)}\nQualification Run: {run_id}\n",
        created_at=utc_now(),
    )
    store.write_immutable("revisions", revision.revision_id, revision)
    store.write_snapshot_value(
        "proposals",
        task.proposal_id,
        {"proposal_id": task.proposal_id, "state": "COMPILED"},
    )
    store.write_immutable("tasks", task.task_id, task)
    store.write_immutable(
        "task-status-history",
        new_id("status"),
        TaskStatus(
            task_id=task.task_id,
            execution_id=task.execution_id,
            state=TaskState(state),
            updated_at=utc_now(),
        ),
    )


def test_reconstruction_finds_every_proposal_bound_to_the_run(runtime_identity):
    """The Agent is not asked for its result; the run_id binding already in the
    evidence graph is used to discover it."""
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_reconstruct"
    for cards in (1, 2, 4, 8):
        _seed_qualification_item(store, identity, run_id, cards)

    result = reconstruct_qualification_result(store, run_id)

    assert result.run_id == run_id
    assert result.status == "COMPLETED", result.reason
    assert sorted(item.card_count for item in result.items) == [1, 2, 4, 8]
    assert all(item.state == "COMPLETED" for item in result.items)


def test_reconstruction_reports_the_missing_card_counts(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_partial"
    for cards in (1, 2):
        _seed_qualification_item(store, identity, run_id, cards)

    result = reconstruct_qualification_result(store, run_id)

    assert result.status == "BLOCKED_QUALIFICATION"
    assert result.reason is not None and "4" in result.reason and "8" in result.reason
    assert sorted(item.card_count for item in result.items) == [1, 2]


def test_reconstruction_ignores_proposals_from_another_run(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    for cards in (1, 2, 4, 8):
        _seed_qualification_item(store, identity, "qual_other", cards)
    _seed_qualification_item(store, identity, "qual_target", 1)

    result = reconstruct_qualification_result(store, "qual_target")

    assert [item.card_count for item in result.items] == [1]


def test_reconstruction_keeps_a_task_that_did_not_complete(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_failed"
    for cards in (1, 2, 4):
        _seed_qualification_item(store, identity, run_id, cards)
    _seed_qualification_item(store, identity, run_id, 8, state="FAILED")

    result = reconstruct_qualification_result(store, run_id)

    assert result.status == "BLOCKED_QUALIFICATION"
    assert {item.card_count: item.state for item in result.items}[8] == "FAILED"
```

`store.write_snapshot_value` 是占位名：`EventStore.write_snapshot` 现有签名接受 pydantic 模型。先看 `src/agent_scheduler/storage/events.py` 里 `write_snapshot` 与 `read_snapshot` 的实际签名——若它只收模型，就直接用 `store.write_snapshot("proposals", proposal_id, proposal)` 并构造一个真实的 `Proposal`（`state=ProposalState.COMPILED`，其余字段照 `tests/conftest.py` 的风格填），不要为测试新增存储 API。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_qualification.py -k reconstruction -v`
Expected: FAIL，`ImportError: cannot import name 'reconstruct_qualification_result'`

- [ ] **Step 3: 写实现**

追加到 `src/agent_scheduler/qualification.py`：

```python
_EXPECTED_CARD_COUNTS = (1, 2, 4, 8)


def reconstruct_qualification_result(store: EventStore, run_id: str) -> QualificationResult:
    """Derive the run's outcome from Ground Truth instead of asking the Agent.

    The four harnesses differ most in structured-output support, and the result is
    only a set of pointers that ``verify_qualification`` re-validates anyway. The
    ``Qualification Run: <run_id>`` binding is already required by ``_verify_item``,
    so reuse it as the discovery mechanism rather than adding an output contract
    that the weakest CLI cannot honour.
    """
    marker = f"Qualification Run: {run_id}"
    revision_to_proposal = {
        str(value["revision_id"]): str(value["proposal_id"])
        for value in store.iter_immutable("revisions")
        if isinstance(value.get("markdown"), str)
        and marker in value["markdown"]
        and isinstance(value.get("revision_id"), str)
        and isinstance(value.get("proposal_id"), str)
    }
    compiled = {
        proposal_id
        for proposal_id in set(revision_to_proposal.values())
        if (snapshot := store.read_snapshot("proposals", proposal_id)) is not None
        and snapshot.get("state") == "COMPILED"
    }
    items: list[QualificationItem] = []
    for value in store.iter_immutable("tasks"):
        task = strict_from_json_value(Task, value)
        if task.proposal_id not in compiled or task.revision_id not in revision_to_proposal:
            continue
        if len(task.units) != 1:
            continue
        card_count = task.units[0].required_gpu_count
        if card_count not in _EXPECTED_CARD_COUNTS:
            continue
        status = _latest_task_status(store, task.task_id)
        items.append(
            QualificationItem(
                card_count=card_count,
                proposal_id=task.proposal_id,
                task_id=task.task_id,
                state=status.state.value if status is not None else "UNKNOWN",
            )
        )
    items.sort(key=lambda item: (item.card_count, item.task_id))
    found = {item.card_count for item in items}
    missing = [count for count in _EXPECTED_CARD_COUNTS if count not in found]
    incomplete = [item.task_id for item in items if item.state != "COMPLETED"]
    if missing:
        return _blocked(
            tuple(items),
            f"run produced no COMPLETED Task for card counts {missing}",
            run_id,
        )
    if incomplete:
        return _blocked(
            tuple(items), f"Tasks did not reach COMPLETED: {incomplete}", run_id
        )
    if len(items) != len(_EXPECTED_CARD_COUNTS):
        return _blocked(
            tuple(items),
            f"run produced {len(items)} Tasks for {len(_EXPECTED_CARD_COUNTS)} card counts",
            run_id,
        )
    return QualificationResult(run_id=run_id, status="COMPLETED", items=tuple(items))
```

`QualificationItem.card_count` 的类型是 `Literal[1, 2, 4, 8]`；上面已按 `_EXPECTED_CARD_COUNTS` 过滤，但 mypy strict 无法推断。在 `card_count not in _EXPECTED_CARD_COUNTS` 的过滤之后，用显式 `cast(Literal[1, 2, 4, 8], card_count)` 并在文件顶部导入 `cast`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_qualification.py -v`
Expected: 全部通过

- [ ] **Step 5: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/agent_scheduler/qualification.py tests/test_qualification.py
git commit -m "feat: reconstruct the qualification result from Ground Truth

Asking the Submitter to echo its result would make structured-output support
a correctness requirement, and that is exactly the axis where the four CLIs
differ most. The run_id binding the evidence check already relies on is
enough to discover the same pointers without any new contract."
```

---

### Task 6: `qualify --harness` 与按 harness 匹配证据

**Files:**
- Modify: `src/agent_scheduler/qualification.py`（`run_submitter_agent`、`verify_qualification`、`_verify_item`）
- Modify: `src/agent_scheduler/cli/main.py:48-50`、`:138-161`
- Test: `tests/test_qualification.py`、`tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4 的 `build_submitter_invocation`、Task 5 的 `reconstruct_qualification_result`
- Produces:
  - `run_submitter_agent(..., harness: str = "claude")` — 新增关键字参数，`executable` 参数默认改为 `None`（按 harness 解析）
  - `verify_qualification(..., harness: str = "claude")` — 新增关键字参数
  - harness 审计记录新增字段 `"harness": str`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_qualification.py`：

```python
def _seed_run_preconditions(
    store: EventStore, run_id: str, *, harness: str | None
) -> QualificationResult:
    """Seed just enough for verification to reach the Submitter-evidence check.

    A full passing evidence graph needs Plans, manifests, worker samples, protocol
    events and on-disk artifacts. The harness binding is decided before any of that,
    so seed up to that point and assert on which check rejects.
    """
    store.write_immutable(
        "qualification-runs",
        run_id,
        {
            "schema_version": "v1",
            "run_id": run_id,
            "started_at": utc_now().isoformat(),
            "profile": {
                "qualification": True,
                "harness_mode": "claude",
                "worker_mode": "remote",
            },
        },
    )
    store.write_immutable(
        "qualification-gates",
        new_id("gate"),
        {
            "schema_version": "v1",
            "run_id": run_id,
            "passed": True,
            "timestamp": utc_now().isoformat(),
            "results": [
                {"argv": ["uv", "run", "pytest", "-q"], "exit_code": 0},
                {"argv": ["uv", "run", "ruff", "check", "."], "exit_code": 0},
                {"argv": ["uv", "run", "mypy", "src"], "exit_code": 0},
            ],
        },
    )
    items = tuple(
        QualificationItem(
            card_count=cards,
            proposal_id=f"prop_{cards}",
            task_id=f"task_{cards}",
            state="COMPLETED",
        )
        for cards in (1, 2, 4, 8)
    )
    invocation_id = new_id("harness")
    record: dict[str, object] = {
        "schema_version": "v1",
        "invocation_id": invocation_id,
        "run_id": run_id,
        "role": "submitter",
        "exit_code": 0,
        "started_at": utc_now().isoformat(),
        "stdout": " ".join(f"{item.proposal_id} {item.task_id}" for item in items),
    }
    if harness is not None:
        record["harness"] = harness
    store.write_immutable("harness", invocation_id, record)
    return QualificationResult(run_id=run_id, status="COMPLETED", items=items)


class _StoppedDocker:
    """Stand in for DockerCLI so verification never shells out in a unit test."""

    def inspect(self, name: str):
        from agent_scheduler.worker.docker import ContainerInspection

        return ContainerInspection(exists=True, running=False)


def test_verification_rejects_submitter_evidence_from_another_harness(runtime_identity):
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_harness", harness="codex")

    rejected = verify_qualification(
        result, state_root=root, identity=identity, harness="pi", docker=_StoppedDocker()
    )

    assert rejected.status == "BLOCKED_QUALIFICATION"
    assert rejected.reason is not None
    assert "pi Submitter evidence is incomplete" in rejected.reason


def test_verification_accepts_submitter_evidence_from_the_named_harness(runtime_identity):
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_harness_ok", harness="codex")

    verified = verify_qualification(
        result, state_root=root, identity=identity, harness="codex", docker=_StoppedDocker()
    )

    # The graph past this point is intentionally absent, so it still blocks — but on a
    # later check, which is what proves the harness binding itself was accepted.
    assert verified.status == "BLOCKED_QUALIFICATION"
    assert verified.reason is not None
    assert "Submitter evidence is incomplete" not in verified.reason


def test_legacy_submitter_evidence_without_a_harness_field_counts_as_claude(
    runtime_identity,
):
    """Evidence recorded before the field existed was produced by Claude Code."""
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_legacy", harness=None)

    verified = verify_qualification(
        result, state_root=root, identity=identity, harness="claude", docker=_StoppedDocker()
    )

    assert verified.reason is not None
    assert "Submitter evidence is incomplete" not in verified.reason
```

`ContainerInspection` 的真实构造签名以 `src/agent_scheduler/worker/docker.py` 为准；若字段名不同，按实际填。补齐导入：`QualificationItem`、`QualificationResult` 从 `agent_scheduler.qualification` 导入，`EventStore`、`new_id`、`utc_now` 沿用 Task 5 已加的导入。

追加到 `tests/test_cli.py`：

```python
def test_qualify_accepts_a_harness_flag():
    from agent_scheduler.cli.main import build_parser

    args = build_parser().parse_args(["qualify", "--harness", "dsh"])
    assert args.harness == "dsh"


def test_qualify_defaults_to_claude():
    from agent_scheduler.cli.main import build_parser

    assert build_parser().parse_args(["qualify"]).harness == "claude"


def test_qualify_rejects_an_unknown_harness():
    import pytest

    from agent_scheduler.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["qualify", "--harness", "gemini"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_cli.py -k harness tests/test_qualification.py -k harness_or_legacy -v`
Expected: FAIL，`AttributeError: 'Namespace' object has no attribute 'harness'`

- [ ] **Step 3: 改 CLI parser**

`src/agent_scheduler/cli/main.py` 第 48-50 行，在 `qualify` 的参数后加：

```python
    qualify.add_argument("--harness", choices=HARNESSES, default="claude")
```

并在顶部导入 `from agent_scheduler.adapters.onboarding import HARNESSES`。

- [ ] **Step 4: 改 `run_submitter_agent` 使用 seam**

在 `src/agent_scheduler/qualification.py` 中：

1. 签名加 `harness: str = "claude"`，把 `executable: str = "claude"` 改为 `executable: str | None = None`。
2. 删除函数内手工拼装 `mcp_config`、`schema`、`allowed`、`command`、`prompt`、`env` 的整段（当前第 105-198 行），替换为：

```python
        run_dir = state_root / "qualification" / run_id
        invocation = build_submitter_invocation(
            harness,
            output_dir=run_dir,
            project_root=project_root,
            state_root=state_root,
            base_url=base_url,
            username="zz_chentian",
            uv_path=Path(uv_path).resolve(),
            tls_certificate=tls_certificate,
            executable=executable,
            run_id=run_id,
        )
        command = list(invocation.argv)
        prompt = invocation.prompt
        env = invocation.env
        cli_version = _harness_version(command[0], env)
```

3. `audit` 字典初始化处（第 70-76 行）加入 `"harness": harness`。
4. 把 `_claude_version` 重命名为 `_harness_version`，其错误信息中的 `Claude ​Code` 改为参数化的可执行名。
5. 重试循环里，成功分支不再解析 stdout，改为：

```python
            return reconstruct_qualification_result(store, run_id)
```

删除 `_stream_structured_output` 及其唯一调用，以及针对结构化输出的 `except (json.JSONDecodeError, TypeError, ValueError)` 重试分支。**保留** exit code 非零时的 `_retryable_submitter_failure` 重试——它是夹具自身的健壮性，不是对 Agent 输出的依赖。
6. 非零 exit code 时不再直接 `_blocked` 返回，而是先重建再判断：Agent 可能做完全部工作后才因非致命原因退出非零。改为：

```python
            if completed.returncode != 0:
                reconstructed = reconstruct_qualification_result(store, run_id)
                if reconstructed.status == "COMPLETED":
                    return reconstructed
                last_reason = (
                    f"Submitter {harness} exited with code {completed.returncode}: "
                    f"{reconstructed.reason}"
                )
                if _retryable_submitter_failure(completed.stderr) and attempt < 4:
                    continue
                return _blocked(reconstructed.items, last_reason, run_id)
```

7. `harness_mode` profile 校验保持不变——它校验的是 Master 的 Processor/Reviewer 模式，与 Submitter 是谁无关。

导入：`from agent_scheduler.adapters.submitter import build_submitter_invocation`。

- [ ] **Step 5: 改证据校验按 harness 匹配**

在 `verify_qualification` 签名加 `harness: str = "claude"`。把当前第 336-345 行的 submitter 证据筛选改为：

```python
        all_harness = list(store.iter_immutable("harness"))
        submitter = [
            item
            for item in all_harness
            if item.get("run_id") == result.run_id
            and item.get("role") == "submitter"
            and item.get("exit_code") == 0
            # Evidence recorded before the field existed came from Claude Code.
            and item.get("harness", "claude") == harness
        ]
        if len(submitter) != 1:
            return _blocked(
                result.items,
                f"current {harness} Submitter evidence is incomplete",
                result.run_id,
            )
```

`_verify_item` 增加 `harness: str` 参数，其内部 `submitter_records` 的筛选加上同一条 `item.get("harness", "claude") == harness`，并把调用处改为 `_verify_item(item, result.run_id, store, identity, all_harness, harness)`。

- [ ] **Step 6: 接上 CLI**

`src/agent_scheduler/cli/main.py` 第 143-152 行，两处调用加 `harness=args.harness`：

```python
            result = run_submitter_agent(
                project_root=project_root,
                state_root=settings.state_root,
                base_url=args.base_url,
                tls_certificate=identity.tls_certificate,
                timeout_seconds=args.timeout,
                harness=args.harness,
            )
            verified = verify_qualification(
                result,
                state_root=settings.state_root,
                identity=identity,
                harness=args.harness,
            )
```

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/test_cli.py tests/test_qualification.py -v`
Expected: 全部通过

- [ ] **Step 8: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 9: 提交**

```bash
git add src/agent_scheduler/qualification.py src/agent_scheduler/cli/main.py tests/test_cli.py tests/test_qualification.py
git commit -m "feat: qualify any of the four Submitter harnesses

Bind submitter evidence to the harness that produced it so each Agent gets
its own verifiable package. Records written before the field existed came
from Claude Code, so treat a missing value as such rather than invalidating
the qualification evidence already on disk."
```

---

### Task 7: T2 通路层测试

真启动每个 agent，证明它能把一个 Proposal 建出来。

**Files:**
- Modify: `pyproject.toml:36-40`（markers）
- Create: `tests/test_real_onboarding.py`

**Interfaces:**
- Consumes: Task 4 的 `build_submitter_invocation`、Task 3 的 `HARNESSES`
- Produces: pytest markers `real_codex`、`real_pi`、`real_dsh`

- [ ] **Step 1: 加 markers**

`pyproject.toml` 的 `[tool.pytest.ini_options] markers` 追加三条：

```toml
  "real_codex: requires an authenticated Codex CLI",
  "real_pi: requires an authenticated pi CLI with pi-mcp-adapter installed",
  "real_dsh: requires an authenticated dsh with dsh-mcp-bridge installed",
```

- [ ] **Step 2: 写测试**

创建 `tests/test_real_onboarding.py`：

```python
"""T2: prove each Agent can actually reach the control plane through its own onboarding.

Opt-in. Requires a running Master; AGENT_SCHEDULER_HARNESS_MODE=fake is enough,
so these cost a few tokens and no GPU time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from agent_scheduler.adapters.submitter import build_submitter_invocation
from agent_scheduler.domain.models import new_id
from agent_scheduler.runtime import load_runtime
from agent_scheduler.storage import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://127.0.0.1:8443"


def _run_submitter(harness: str, tmp_path: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    state_root = Path(
        os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
    )
    identity = load_runtime(state_root)
    response = httpx.get(
        f"{BASE_URL}/health", verify=str(identity.tls_certificate), timeout=10
    )
    response.raise_for_status()
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv is required to launch the MCP adapter"
    invocation = build_submitter_invocation(
        harness,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=state_root,
        base_url=BASE_URL,
        username="zz_chentian",
        uv_path=Path(uv_path).resolve(),
        tls_certificate=identity.tls_certificate,
        run_id=run_id,
    )
    prompt = (
        f"{invocation.prompt}\n\n"
        "For this connectivity check, create exactly ONE 1-card Proposal and stop. "
        "Do not confirm it, do not poll for a Task."
    )
    return subprocess.run(
        list(invocation.argv),
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=15 * 60,
        cwd=PROJECT_ROOT,
        env=invocation.env,
    )


def _created_proposal_for(state_root: Path, run_id: str) -> bool:
    marker = f"Qualification Run: {run_id}"
    return any(
        isinstance(value.get("markdown"), str) and marker in value["markdown"]
        for value in EventStore(state_root).iter_immutable("revisions")
    )


@pytest.mark.parametrize(
    ("harness", "marker_env"),
    [
        pytest.param("claude", "RUN_REAL_CLAUDE", marks=pytest.mark.real_claude),
        pytest.param("codex", "RUN_REAL_CODEX", marks=pytest.mark.real_codex),
        pytest.param("pi", "RUN_REAL_PI", marks=pytest.mark.real_pi),
        pytest.param("dsh", "RUN_REAL_DSH", marks=pytest.mark.real_dsh),
    ],
)
def test_harness_creates_a_proposal_through_its_own_onboarding(
    harness: str, marker_env: str, tmp_path: Path
):
    if os.environ.get(marker_env) != "1":
        pytest.skip(f"set {marker_env}=1 for a billed {harness} invocation")
    state_root = Path(
        os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
    )
    run_id = new_id("t2")

    completed = _run_submitter(harness, tmp_path, run_id)

    # A created Proposal is the only assertion that holds across all four: pi hides
    # the 12 tools behind a proxy tool unless directTools is honoured, so asserting
    # native tool names would report a false failure. Creating a Proposal proves tool
    # discovery, argument passing, and control-plane connectivity at once.
    assert _created_proposal_for(state_root, run_id), (
        f"{harness} created no Proposal bound to {run_id}\n"
        f"exit={completed.returncode}\nstdout={completed.stdout[-4000:]}\n"
        f"stderr={completed.stderr[-4000:]}"
    )
```

- [ ] **Step 3: 确认默认门禁跳过这些测试**

Run: `uv run pytest -q`
Expected: 新测试全部 skip（未设 `RUN_REAL_*`），其余通过

- [ ] **Step 4: 起 Master 后逐个真跑**

三个终端：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=fake
export AGENT_SCHEDULER_WORKER_MODE=remote
uv run agent-scheduler serve     # 终端 1
uv run agent-scheduler worker    # 终端 2
```

终端 3 逐个跑，一次一个，失败就停下排查再继续：

```bash
RUN_REAL_CLAUDE=1 uv run pytest tests/test_real_onboarding.py -m real_claude -v
RUN_REAL_CODEX=1  uv run pytest tests/test_real_onboarding.py -m real_codex -v
RUN_REAL_PI=1     uv run pytest tests/test_real_onboarding.py -m real_pi -v
RUN_REAL_DSH=1    uv run pytest tests/test_real_onboarding.py -m real_dsh -v
```

Spec §7 列的三项前置在此暴露：codex 未登录、pi 的 provider 默认是 `google`、dsh 的 approval 策略。哪一项失败就先解决哪一项——codex 若 `OPENAI_API_KEY` 路不通，在 `build_submitter_invocation` 的 codex 分支补 `-c model_provider=...` 配置；pi 若报无可用 provider，补 `--provider`/`--model` 参数。每处修改都要回到 Task 4 的测试文件加一条断言再改实现。

- [ ] **Step 5: 记录实际结果**

把四个 harness 的通路层结论（通过 / 失败原因）写进 `docs/qualification-status.md` 的新章节「T2 通路层」。

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml tests/test_real_onboarding.py src/agent_scheduler/adapters/submitter.py tests/test_submitter_harness.py docs/qualification-status.md
git commit -m "test: prove each harness reaches the control plane through its own onboarding

Assert on a created Proposal rather than on visible tool names: pi keeps the
12 tools behind a proxy tool, so a name-based assertion would fail a working
setup. One Proposal covers discovery, argument passing, and connectivity."
```

---

### Task 8: 四家接入文档

**Files:**
- Modify: `docs/submitting-from-a-claude-session.md` → 重命名为 `docs/submitting-from-an-agent-session.md`
- Modify: `README.md:19`
- Modify: `docs/usage.md`（交叉引用）
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: Task 3 的 `build_onboarding`
- Produces: 无新符号

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_onboarding.py`：

```python
def test_onboarding_doc_covers_every_harness():
    doc = (PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md").read_text(
        encoding="utf-8"
    )
    for harness in HARNESSES:
        assert harness in doc.lower(), f"onboarding doc does not mention {harness}"
    # The one-time installs are the接入方's responsibility and must be stated.
    assert "pi install npm:pi-mcp-adapter" in doc
    assert "dsh plugin" in doc and "dsh-mcp-bridge" in doc


def test_no_document_still_points_at_the_old_claude_only_filename():
    stale = []
    for path in PROJECT_ROOT.glob("**/*.md"):
        if ".claude/worktrees" in str(path) or ".venv" in str(path):
            continue
        if "submitting-from-a-claude-session" in path.read_text(encoding="utf-8"):
            stale.append(str(path.relative_to(PROJECT_ROOT)))
    assert stale == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_onboarding.py -k doc -v`
Expected: FAIL，文件不存在

- [ ] **Step 3: 重命名并扩写**

```bash
git mv docs/submitting-from-a-claude-session.md docs/submitting-from-an-agent-session.md
```

改写内容：

1. 标题与首段改为「从一个 Agent 会话提交 Proposal」，覆盖四家。
2. 开头那张表改为两者皆必须：

   | | 作用 | 必要性 | 不装的后果 |
   | --- | --- | --- | --- |
   | MCP Server | **通路** | 必须 | 会话没有可用工具 |
   | Skill | **知识** | **必须** | 工具能调，但内容几乎必然被 `422` 拒绝 |

   保留原文里那段「首次真实资格运行连续 7 次被拒」的实例——它是这条规则的证据。
3. 「步骤 2 · 给会话装 MCP Server」拆成四个小节，每节给可直接复制的命令。内容以 `build_onboarding()` 的输出为准（`uv run python -c "..."` 打印一份即可核对）。
4. pi 小节必须写清两件事：`pi install npm:pi-mcp-adapter` 是一次性前置；不配 `directTools` 时 12 个工具藏在 `mcp({tool: ...})` 代理工具后面，仍可用但多一层转述。
5. dsh 小节写清 `dsh plugin --profile headless add dsh-mcp-bridge` 是一次性前置，并注明 `dsh-mcp-bridge` 是第三方包、其依赖 `@deepseek-ai/dsh-mcp-client` 是官方实现，可作为备选。
6. 「步骤 3 · 装 Skill」改为说明 skill 现位于 `.agents/skills/submit-gpu-task/`，codex 与 pi 原生发现，Claude ​Code 经 `.claude/skills/` 的 symlink 发现，dsh 经 patch overlay 指向。
7. 排障表补三行：codex `Not logged in`、pi 未指定 provider、dsh approval 卡住。

Spec §3 曾提到「`config/` 下新增 codex、dsh 的配置模板」。这里改为文档内嵌、内容以
`build_onboarding()` 输出为准：codex 的接入是一串 `-c` flag 而非文件，落成 `config/` 里的
文件反而要求接入方去反推命令；dsh 的 patch 是每轮生成的。既有的
`config/submitter-mcp.example.json` 保留不动，仍服务 claude 与 pi。

- [ ] **Step 4: 更新交叉引用**

`README.md` 第 19 行的链接与文字改为新文件名与「从 Agent 会话提交」。`docs/usage.md` 中所有指向旧文件名处一并更新。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: 全部通过

- [ ] **Step 6: 确认门禁**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add docs README.md tests/test_onboarding.py
git commit -m "docs: cover all four Agent harnesses in the onboarding guide

The skill moves from strongly recommended to required: an Agent that has the
tools but not the contract gets 422'd, which the first real qualification run
demonstrated seven times in a row."
```

---

### Task 9: T3 端到端资格

每个 harness 完整跑 1/2/4/8 并过 `verify_qualification()`。

**Files:**
- Modify: `tests/test_real_qualification.py:44-58`
- Modify: `docs/qualification-status.md`

**Interfaces:**
- Consumes: Task 6 的 `run_submitter_agent(harness=...)`、`verify_qualification(harness=...)`
- Produces: 无新符号

- [ ] **Step 1: 参数化端到端测试**

把 `tests/test_real_qualification.py` 中的 `test_complete_real_qualification` 替换为：

```python
@pytest.mark.parametrize(
    ("harness", "marker_env"),
    [
        pytest.param("claude", "RUN_REAL_CLAUDE", marks=pytest.mark.real_claude),
        pytest.param("codex", "RUN_REAL_CODEX", marks=pytest.mark.real_codex),
        pytest.param("pi", "RUN_REAL_PI", marks=pytest.mark.real_pi),
        pytest.param("dsh", "RUN_REAL_DSH", marks=pytest.mark.real_dsh),
    ],
)
@pytest.mark.real_gpu
def test_complete_real_qualification(harness: str, marker_env: str):
    if os.environ.get("RUN_FULL_QUALIFICATION") != "1":
        pytest.skip("set RUN_FULL_QUALIFICATION=1 after Master and Worker are running")
    if os.environ.get(marker_env) != "1":
        pytest.skip(f"set {marker_env}=1 to qualify the {harness} Submitter")
    root = Path(os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp"))
    identity = load_runtime(root)
    result = run_submitter_agent(
        project_root=Path(__file__).resolve().parents[1],
        state_root=root,
        base_url="https://127.0.0.1:8443",
        tls_certificate=identity.tls_certificate,
        harness=harness,
    )
    verified = verify_qualification(
        result, state_root=root, identity=identity, harness=harness
    )
    assert verified.status == "COMPLETED", verified.reason
```

- [ ] **Step 2: 确认默认门禁仍跳过**

Run: `uv run pytest -q`
Expected: 全部通过，端到端测试 skip

- [ ] **Step 3: 起真实环境**

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=claude
export AGENT_SCHEDULER_WORKER_MODE=remote
uv run agent-scheduler serve     # 终端 1
uv run agent-scheduler worker    # 终端 2
```

确认 `curl -sk https://127.0.0.1:8443/health` 的 `workers` 为 `1`、`integrity` 为 `valid`。

- [ ] **Step 4: 逐个 harness 跑完整资格**

复用容器严格串行，**一次只跑一个 harness**，跑完确认容器 stopped 再跑下一个：

```bash
export RUN_FULL_QUALIFICATION=1
RUN_REAL_CLAUDE=1 RUN_REAL_GPU=1 uv run pytest tests/test_real_qualification.py -k claude -v
docker inspect -f '{{.State.Running}}' fh-sglang-deepseek-v4-flash   # 必须是 false

RUN_REAL_CODEX=1 RUN_REAL_GPU=1 uv run pytest tests/test_real_qualification.py -k codex -v
docker inspect -f '{{.State.Running}}' fh-sglang-deepseek-v4-flash

RUN_REAL_PI=1 RUN_REAL_GPU=1 uv run pytest tests/test_real_qualification.py -k "pi" -v
docker inspect -f '{{.State.Running}}' fh-sglang-deepseek-v4-flash

RUN_REAL_DSH=1 RUN_REAL_GPU=1 uv run pytest tests/test_real_qualification.py -k dsh -v
docker inspect -f '{{.State.Running}}' fh-sglang-deepseek-v4-flash
```

任一 harness 返回 `BLOCKED_QUALIFICATION` 时，`reason` 就是准确的缺口描述（Task 5 的重建逻辑保证）。修复后重跑该 harness，不要跳过。

- [ ] **Step 5: 记录真实证据**

在 `docs/qualification-status.md` 中为每个 harness 记录：`run_id`、四个 `proposal_id`/`task_id`、终态、`verify_qualification` 结论。失败的 harness 如实记为 `BLOCKED_QUALIFICATION` 并写明原因——**代码与 Fake 门禁通过不代表真实资格完成**，这是仓库既有规矩。

- [ ] **Step 6: 提交**

```bash
git add tests/test_real_qualification.py docs/qualification-status.md
git commit -m "test: qualify every Submitter harness end to end

Each Agent now produces its own evidence package rather than inheriting
Claude Code's. Real results recorded per harness, including any that are
BLOCKED_QUALIFICATION."
```

---

## 依赖关系

```
Task 1 (skill 搬迁 + onboarding.py 骨架)
  └─> Task 3 (配置生成器)
        └─> Task 4 (harness seam)
              ├─> Task 6 (qualify --harness)  <── Task 5 (结果重建)
              │     └─> Task 9 (T3 端到端)
              └─> Task 7 (T2 通路层)
        └─> Task 8 (接入文档)

Task 2 (错误消息) 独立，可与任意任务并行
Task 5 (结果重建) 只依赖既有代码，可与 Task 3/4 并行
```

Task 7 必须在 Task 9 之前完成——通路层失败在 T2 就该暴露，不该等到花掉 GPU 时间才发现。
