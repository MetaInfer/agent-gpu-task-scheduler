import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_DOC = PROJECT_ROOT / "docs" / "submitting-from-an-agent-client.md"

FORBIDDEN = (
    "/public/share/fh/agent-gpu-task-scheduler",
    "python3 -m agent_scheduler.cli.main",
    "build_onboarding",
    "AGENT_SCHEDULER_HARNESS_MODE",
    "ANTHROPIC_AUTH_TOKEN",
    "reload-users",
    "init-runtime",
    "ss -lntp",
)

REQUIRED = (
    "python3 -m venv",
    "--no-index",
    "--find-links",
    "sha256sum -c SHA256SUMS",
    "submit-gpu-task",
    "--ca-file",
    "Claude Code",
    "Codex CLI",
    "pi",
    "dsh",
    "tools/list",
    "12",
    "联系服务方",
)

EXPECTED_HEADINGS = (
    "# 从 Agent Client 提交 GPU Task",
    "## 适用读者与边界",
    "## 你应该已经收到什么",
    "## 容器前置条件",
    "## 步骤 1 · 校验 Client Kit",
    "## 步骤 2 · 离线安装 client wheel",
    "## 步骤 3 · 安装 submit-gpu-task skill",
    "## 步骤 4 · 填写连接配置",
    "## 步骤 5 · 选择一个 Agent harness",
    "### Claude Code",
    "### Codex CLI",
    "### pi",
    "### dsh",
    "## 步骤 6 · 分层预检",
    "## 步骤 7 · 触发任务",
    "## Agent 会执行什么",
    "## 客户端排障",
    "## 安全边界",
)

RENDERER_NAMES = (
    "CLIENT_ENTRYPOINT",
    "MASTER_URL",
    "USERNAME",
    "CA_FILE",
    "CLIENT_WORKSPACE",
)

PREFLIGHT_COMMANDS = (
    "sha256sum -c SHA256SUMS",
    'test -x "$CLIENT_ENTRYPOINT"',
    'test -r "$CA_FILE"',
    'curl --cacert "$CA_FILE" "$MASTER_URL/health"',
)

ARTIFACT_FILE_CHECKS = (
    'test -r "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task/SKILL.md"',
    'test -r "$RENDERED_CONFIG"',
)

LAUNCH_COMMANDS = (
    'claude --strict-mcp-config --mcp-config "$RENDERED_CONFIG"',
    "codex \\",
    'pi --mcp-config "$RENDERED_CONFIG"',
    'dsh --profile headless --patch "$RENDERED_CONFIG"',
)

_SHORT_TLS_BYPASS = re.compile(r"(?<!\S)-[A-Za-z]*k[A-Za-z]*(?=\s|$)")


def _client_text() -> str:
    return CLIENT_DOC.read_text(encoding="utf-8")


def _markdown_headings(text: str) -> tuple[str, ...]:
    headings = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.fullmatch(r"#{1,6} .+", line):
            headings.append(line)
    return tuple(headings)


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(next_heading, start)]


def _renderer_names(text: str) -> tuple[str, ...]:
    match = re.search(r"names = \(\n(?P<body>.*?)\n\)\nmissing =", text, re.DOTALL)
    assert match is not None
    return tuple(re.findall(r'^\s+"([A-Z_]+)",$', match.group("body"), re.MULTILINE))


def _assert_release_token_contract(text: str) -> None:
    assert text.count("@@KIT_VERSION@@") == 1
    assert re.findall(r"@@[^@\n]+@@", text) == ["@@KIT_VERSION@@"]


def _assert_no_insecure_tls(text: str) -> None:
    assert "--insecure" not in text
    assert _SHORT_TLS_BYPASS.search(text) is None


def _assert_launch_contract(text: str) -> None:
    step_5 = _section(
        text,
        "## 步骤 5 · 选择一个 Agent harness",
        "## 步骤 6 · 分层预检",
    )
    step_6 = _section(text, "## 步骤 6 · 分层预检", "## 步骤 7 · 触发任务")

    for command in LAUNCH_COMMANDS:
        assert command not in step_5
        assert step_6.count(command) == 1

    preflight_positions = tuple(step_6.index(command) for command in PREFLIGHT_COMMANDS)
    assert preflight_positions == tuple(sorted(preflight_positions))
    file_check_positions = tuple(
        step_6.index(command) for command in ARTIFACT_FILE_CHECKS
    )
    assert (
        preflight_positions[2]
        < file_check_positions[0]
        < file_check_positions[1]
        < preflight_positions[3]
    )
    preflight_end = preflight_positions[-1] + len(PREFLIGHT_COMMANDS[-1])
    tools_list = step_6.index('"method":"tools/list"')
    harness_check = step_6.index("command -v")
    launch_cwd = step_6.index('cd "$CLIENT_WORKSPACE"')
    assert preflight_end < tools_list < harness_check < launch_cwd
    assert 'cd "$CLIENT_WORKSPACE"\n\ncase "$HARNESS" in' in step_6
    for command in LAUNCH_COMMANDS:
        assert launch_cwd < step_6.index(command)


def test_client_doc_is_standalone_and_contains_only_client_actions():
    text = _client_text()
    for value in FORBIDDEN:
        assert value not in text
    for value in REQUIRED:
        assert value in text


def test_client_doc_has_the_exact_heading_order():
    assert _markdown_headings(_client_text()) == EXPECTED_HEADINGS


def test_client_doc_uses_exactly_one_release_version_token():
    _assert_release_token_contract(_client_text())


def test_client_doc_renderer_uses_exactly_the_five_connection_names():
    assert _renderer_names(_client_text()) == RENDERER_NAMES


def test_client_doc_preflights_before_launching_from_client_workspace():
    _assert_launch_contract(_client_text())


def test_client_doc_uses_direct_python3_and_rejects_insecure_tls_commands():
    text = _client_text()
    bash = "\n".join(re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL))
    assert "python3 -m venv" in bash
    assert 'python3 - "$SOURCE_TEMPLATE" "$RENDERED_CONFIG"' in bash
    assert re.search(r"(?m)^\s*uv(?:\s|$)", bash) is None
    _assert_no_insecure_tls(text)


def test_provider_doc_points_client_readers_to_the_client_doc():
    text = (PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md").read_text(
        encoding="utf-8"
    )
    assert "服务提供方内部" in text
    assert "submitting-from-an-agent-client.md" in text


def test_internal_submitter_test_doc_describes_source_isolation():
    text = (PROJECT_ROOT / "docs" / "testing-the-submitter.md").read_text(
        encoding="utf-8"
    )
    assert "agent-scheduler-submitter" in text
    assert "client workspace" in text
    assert "不挂载服务端仓库" in text
    assert "python3 -m mypy src packages/client/src" in text
