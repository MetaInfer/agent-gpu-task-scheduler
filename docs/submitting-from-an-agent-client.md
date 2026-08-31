# 从 Agent Client 提交 GPU Task

## 适用读者与边界

本文只面向在容器中运行 Agent Client 的操作者。你将使用服务方交付的 Client Kit，
让 Claude Code、Codex CLI、pi 或 dsh 通过同一个 `submitter` MCP Server 提交 GPU Task。

这份文档可以脱离项目仓库独立使用。你不需要调度器服务端源码，也不负责部署、启动或诊断
服务端组件。凡是客户端检查无法解决的服务端或网络问题，都应联系服务方。

## 你应该已经收到什么

开始前，确认服务方已经为本次部署交付或约定以下内容：

- 一个完整、版本化的 Client Kit，解压到 `/opt/agent-client/kit`；其中应有 `wheels/`、
  `skills/submit-gpu-task/`、`config/`、`docs/`、`MANIFEST.json` 和 `SHA256SUMS`。
- HTTPS 地址 `MASTER_URL`、允许使用的 `USERNAME`、容器内只读 CA 文件路径 `CA_FILE`，
  以及本次会话使用的 `CLIENT_WORKSPACE`。
- 四种 Agent harness 之一：Claude Code、Codex CLI、pi 或 dsh。只需要选择一种。

如果 Kit 不完整、校验不通过，或任一部署值未发放，请停止并联系服务方，不要自行猜测。

## 容器前置条件

容器应具备：

- Python 3.10 或更高版本，并包含 `venv` 和 `pip`；
- `sha256sum` 与支持 `--cacert` 的 `curl`；
- 选定的 Agent harness，并且该 harness 已通过其自身获准的机制完成模型认证；MCP 配置不负责
  harness 登录，任何 API key、token 或登录材料都不得写入 Kit、渲染配置、skill 或故障报告；
- 对 `/opt/agent-client` 和 `CLIENT_WORKSPACE` 的写权限；
- 对服务方 HTTPS 地址的网络访问，以及对发放 CA 文件的只读访问。

Client wheel 从 Kit 的本地 wheelhouse 离线安装；安装这一步不需要访问软件包索引。

## 步骤 1 · 校验 Client Kit

在 Kit 根目录校验每个交付文件：

```bash
cd /opt/agent-client/kit
python3 verify_client_kit.py .
sha256sum -c SHA256SUMS
```

`verify_client_kit.py` 仅使用 Python 标准库；它不仅验证 manifest 与每个 SHA-256，还验证完整普通
文件集合，拒绝额外文件、缺失文件、symlink 和特殊文件。`sha256sum -c` 是额外的人类可读摘要检查，
不能替代完整集合验证。只有两项都成功才继续。任何缺失、额外文件或摘要不一致都表示交付物不完整
或已变化；不要修改 `SHA256SUMS` 来绕过失败，应联系服务方更换 Kit。

## 步骤 2 · 离线安装 client wheel

创建专用虚拟环境，并且只从 Kit wheelhouse 安装：

```bash
python3 -m venv /opt/agent-client/venv
/opt/agent-client/venv/bin/python3 -m pip install \
  --no-index \
  --find-links /opt/agent-client/kit/wheels \
  "agent-gpu-task-scheduler-client==@@KIT_VERSION@@"
```

版本由 Client Kit release 固定，不要改成“最新版本”，也不要混入其他来源的同名包。

## 步骤 3 · 安装 submit-gpu-task skill

把 Kit 内的 canonical skill 复制到 workspace，再建立 Claude Code 使用的同一个相对 symlink。
目标位置必须尚不存在，以免把不同 Kit 版本混在一起：

```bash
export CLIENT_WORKSPACE='/workspace'

mkdir -p \
  "$CLIENT_WORKSPACE/.agents/skills" \
  "$CLIENT_WORKSPACE/.claude/skills"
test ! -e "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task"
test ! -L "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task"
test ! -e "$CLIENT_WORKSPACE/.claude/skills/submit-gpu-task"
test ! -L "$CLIENT_WORKSPACE/.claude/skills/submit-gpu-task"
cp -R \
  /opt/agent-client/kit/skills/submit-gpu-task \
  "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task"
ln -s \
  ../../.agents/skills/submit-gpu-task \
  "$CLIENT_WORKSPACE/.claude/skills/submit-gpu-task"
test -r "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task/SKILL.md"
```

Codex CLI 与 pi 从 `.agents/skills` 发现 skill；Claude Code 跟随 `.claude/skills` 下的相对
symlink；dsh 由稍后的 patch 把同一个 `.agents/skills` 根加入会话。不要把 skill 中的 Proposal
模板抄到其他位置另行维护。

## 步骤 4 · 填写连接配置

不要照抄任何示例值。把以下所有示例值替换为服务方针对此部署发放或约定的实际值。
如果使用本指南的标准安装位置，`CLIENT_ENTRYPOINT` 的实际值就是下面这个确定性绝对路径：

```bash
export MASTER_URL='https://master.example:8443'
export USERNAME='client_user-1'
export CA_FILE='/shared/agent-scheduler-mvp/tls/certificate.pem'
export CLIENT_WORKSPACE='/workspace'
export CLIENT_ENTRYPOINT='/opt/agent-client/venv/bin/agent-scheduler-submitter'
```

选定步骤 5 的一个 harness 后，先按该分支设置 `SOURCE_TEMPLATE` 和 `RENDERED_CONFIG`，
再运行下面的 renderer。五个 provider value 的允许契约是非空单行值，且不得包含双引号、反斜杠或
ASCII control character；这让同一动态 marker 能安全写入 JSON、TOML 与 dsh YAML。renderer 只替换
允许的五个名称，并拒绝不安全值及任何未解析 token：

```bash
render_client_config() {
  python3 - "$SOURCE_TEMPLATE" "$RENDERED_CONFIG" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
names = (
    "CLIENT_ENTRYPOINT",
    "MASTER_URL",
    "USERNAME",
    "CA_FILE",
    "CLIENT_WORKSPACE",
)
missing = [name for name in names if not os.environ.get(name)]
if missing:
    raise SystemExit(f"missing required values: {', '.join(missing)}")
unsafe = [
    name
    for name in names
    if any(
        character in {'"', "\\"} or ord(character) < 32
        for character in os.environ[name]
    )
]
if unsafe:
    raise SystemExit(
        "provider values must not contain a quote, backslash, or control character: "
        + ", ".join(unsafe)
    )
marker = "@" * 2
text = source.read_text(encoding="utf-8")
for name in names:
    text = text.replace(f"{marker}{name}{marker}", os.environ[name])
if marker in text:
    raise SystemExit("rendered config still contains an unresolved token")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(text, encoding="utf-8")
PY
}
```

渲染结果应留在本次 workspace 内，不要覆盖 Kit 中经过 hash 校验的源模板。

## 步骤 5 · 选择一个 Agent harness

以下四个配置分支只执行一个。每个分支都设置 `HARNESS`、从 Client Kit 的 `config/` 目录
选择模板，并调用步骤 4 定义的 renderer。**本步骤不启动 harness**；所有预检通过后才在
步骤 6 从 `CLIENT_WORKSPACE` 启动所选进程。

### Claude Code

共享 JSON 模板包含 stdio MCP 参数和 12 个 `directTools` 名称。Claude Code 启动时会使用
严格的一次性配置，不读取其他 MCP 配置：

```bash
export HARNESS='claude'
export SOURCE_TEMPLATE='/opt/agent-client/kit/config/mcp.example.json'
export RENDERED_CONFIG="$CLIENT_WORKSPACE/.client-config/claude-mcp.json"
render_client_config
```

### Codex CLI

TOML 模板本身就是启动时实际生效的配置——不是人读的参考,也不会被第二次转成 `-c` 参数。
渲染完成后，把它当作一份全新、独立的 `config.toml`，放进一个专用的 `CODEX_HOME`
目录（不是 `~/.codex`）；步骤 6 启动 codex 时只要这个环境变量还在 shell 里，codex 就会
从这份文件加载 `mcp_servers.submitter`。不运行任何会写入用户级全局配置
（`~/.codex/config.toml`）的 MCP 添加命令：

```bash
export HARNESS='codex'
export SOURCE_TEMPLATE='/opt/agent-client/kit/config/codex-mcp.example.toml'
REAL_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export CODEX_HOME="$CLIENT_WORKSPACE/.client-config/codex-home"
export RENDERED_CONFIG="$CODEX_HOME/config.toml"
render_client_config
if [ -f "$REAL_CODEX_HOME/auth.json" ]; then
  cp "$REAL_CODEX_HOME/auth.json" "$CODEX_HOME/auth.json"
fi
if [ -f "$REAL_CODEX_HOME/config.toml" ]; then
  awk 'BEGIN{skip=0} /^\[/{skip=($0 ~ /^\[\[?mcp_servers/)} !skip' \
    "$REAL_CODEX_HOME/config.toml" > "$RENDERED_CONFIG.inherited"
  cat "$RENDERED_CONFIG.inherited" "$RENDERED_CONFIG" > "$RENDERED_CONFIG.merged"
  mv "$RENDERED_CONFIG.merged" "$RENDERED_CONFIG"
  rm -f "$RENDERED_CONFIG.inherited"
fi
```

如果你之前用 `codex login` 认证过，上面这一步会把真实（当前生效）`CODEX_HOME` 下的
`auth.json` 复制一份进新目录，让登录态继续可用——**复制，不要移动或软链接**，原文件
保持不动。用 `OPENAI_API_KEY`（或对应厂商网关）认证的场景不需要这一步——那条路径不依赖
`CODEX_HOME` 的文件内容。

同样重要：如果你在 `~/.codex/config.toml` 里配置过 provider/model（例如自定义
`base_url`、`model_provider` 或 `[model_providers.*]` 表），新目录里的 `config.toml`
必须把它们**继承**进来，否则 codex 会退回默认端点。上面的 `awk` 把真实配置里
`mcp_servers` 开头的表剥离后（那份表归 Kit 模板所有，避免 TOML 重复表报错），整体放在
渲染模板**前面**——模板写在后面，`mcp_servers.submitter` 以 Kit 渲染值为准。

### pi

`pi-mcp-adapter` 必须已经包含在客户端镜像中，或由客户从批准的软件源预先安装；它不由
Client Kit 隐式分发。pi 使用与 Claude Code 相同的 JSON 模板，其中 `directTools` 把 12 个
MCP 工具直接暴露给 Agent：

```bash
export HARNESS='pi'
export SOURCE_TEMPLATE='/opt/agent-client/kit/config/mcp.example.json'
export RENDERED_CONFIG="$CLIENT_WORKSPACE/.client-config/pi-mcp.json"
render_client_config
```

### dsh

`dsh-mcp-bridge` 所需的 MCP client 与 skill-filesystem 外部插件必须已经包含在客户端镜像中，
或由客户从批准的软件源预先安装；Client Kit 只提供配置模板。YAML 模板是一份 overlay，
同时声明 submitter MCP 和 `$CLIENT_WORKSPACE/.agents/skills` skill 根：

```bash
export HARNESS='dsh'
export SOURCE_TEMPLATE='/opt/agent-client/kit/config/dsh-mcp.example.patch.yml'
export RENDERED_CONFIG="$CLIENT_WORKSPACE/.client-config/dsh-mcp.patch.yml"
render_client_config
```

后续 dsh 只传这一份 `--patch` overlay，避免 MCP 与 skill 指向不同 workspace。

## 步骤 6 · 分层预检

先回到 Kit 根目录，按顺序验证 artifact、客户端文件与 HTTPS。任何一步失败都不要启动
harness 或触发任务：

```bash
cd /opt/agent-client/kit
python3 verify_client_kit.py .
sha256sum -c SHA256SUMS
test -x "$CLIENT_ENTRYPOINT"
test -r "$CA_FILE"
test -r "$CLIENT_WORKSPACE/.agents/skills/submit-gpu-task/SKILL.md"
test -r "$RENDERED_CONFIG"
curl --fail-with-body --cacert "$CA_FILE" "$MASTER_URL/health"
```

然后直接向本地 entrypoint 发送 JSON-RPC `initialize` 和 `tools/list`：

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
| "$CLIENT_ENTRYPOINT" \
    --base-url "$MASTER_URL" \
    --username "$USERNAME" \
    --ca-file "$CA_FILE"
```

预期返回以下 12 个语义名称：

```text
create_proposal  reply  confirm_revision  get_reviews
resume  cancel  get_proposal  get_task
cancel_task  wait_for_task  wait_for_events  get_logs
```

本地 `initialize` 和 `tools/list` 由 Adapter 直接回答，**不会访问 REST**；它们通过只能证明
entrypoint 与工具契约可用，不能替代前面的 HTTPS health 检查。

启动前再检查所选 harness 的可执行文件。Claude Code 还必须保留步骤 3 创建的项目内 symlink；
pi 与 dsh 的外部插件前置仍由客户端镜像负责：

```bash
case "$HARNESS" in
  claude)
    command -v claude >/dev/null
    test -L "$CLIENT_WORKSPACE/.claude/skills/submit-gpu-task"
    ;;
  codex)
    command -v codex >/dev/null
    ;;
  pi)
    command -v pi >/dev/null
    ;;
  dsh)
    command -v dsh >/dev/null
    ;;
  *)
    printf 'unsupported harness: %s\n' "$HARNESS" >&2
    exit 2
    ;;
esac
```

以上预检全部通过后，先把 **harness 进程本身** 的工作目录切到 `CLIENT_WORKSPACE`，再只启动
步骤 5 选定的一个分支：

```bash
cd "$CLIENT_WORKSPACE"

case "$HARNESS" in
  claude)
    claude --strict-mcp-config --mcp-config "$RENDERED_CONFIG"
    ;;
  codex)
    codex  # reads $CODEX_HOME/config.toml (no manual -c overrides)
    ;;
  pi)
    pi --mcp-config "$RENDERED_CONFIG"
    ;;
  dsh)
    dsh --profile headless --patch "$RENDERED_CONFIG"
    ;;
esac
```

`cd` 让 Claude Code、Codex CLI 与 pi 从项目根发现 `.agents/skills`（Claude Code 跟随其
`.claude/skills` symlink）；Codex 的 MCP `cwd` 只设置 Adapter 子进程目录，不能替代 harness
进程的项目目录。dsh 仍从同一 overlay 取得 MCP 与绝对 skill 根。进入会话后确认 `submitter`
MCP 已连接且 `submit-gpu-task` skill 可见，再继续步骤 7。

## 步骤 7 · 触发任务

在已通过预检的 Agent 会话中输入自然语言意图，同时带上服务方发放的 username 作为纵深防御：

```text
使用配置中显示的 submitter username client_user-1，用 4 张卡提交一个 GPU 任务，并等待最终结果。
```

把示例中的 `client_user-1` 替换为当前配置实际显示的 `USERNAME`。不要让 Agent 猜 username。

## Agent 会执行什么

`submit-gpu-task` skill 会引导四种 harness 走同一流程：

```text
create_proposal
→ confirm_revision
→ 如需修改，get_reviews → reply → confirm_revision
→ wait_for_task
→ 报告最终状态
```

评审要求修改是正常流程，不表示连接失败。完整 Proposal 契约只维护在 Kit 中同版本的 skill 和
reference template 内；本文不复制该模板，以免两个版本漂移。

## 客户端排障

| 现象 | 客户端动作 |
| --- | --- |
| `sha256sum` 失败 | 停止使用该 Kit，联系服务方重新交付；不要改摘要文件 |
| 离线 wheel 安装失败 | 检查 Python 版本、Kit hash 与 `wheels/` 是否完整 |
| entrypoint 不存在或不可执行 | 检查虚拟环境路径和安装输出，然后重新执行离线安装 |
| skill 未被发现 | 检查 canonical 目录、Claude 相对 symlink，或 dsh overlay 中的 skill 根 |
| MCP process failed | 核对 entrypoint 绝对路径、参数、workspace 与渲染结果 |
| Agent harness 未认证 | 通过该 harness 自身获准的登录机制完成认证；不要把凭据写入或发送给 MCP、Kit、skill 或服务方 |
| CA 文件不可读 | 检查容器内只读挂载路径和文件权限；无法修复时联系服务方 |
| TLS hostname mismatch | 核对发放的 URL 与 CA 是否属于同一部署，然后联系服务方；不得关闭 TLS 验证 |
| 连接被拒绝 | 联系服务方确认服务可用性；客户端不执行服务端诊断 |
| 请求 timeout | 联系服务方并提供发生时间、目标 URL 和客户端错误信息 |
| HTTPS health 失败 | 保留 `curl` 输出并联系服务方 |
| `403 USERNAME_NOT_ALLOWED` | 核对配置中的发放 username；若无误，联系服务方 |
| `422 INVALID_PROPOSAL` | 确认 skill 与 Kit 版本一致，并让 Agent 依据返回的 message 修订 |
| `CHANGES_REQUESTED` | 这是正常评审流程，让 Agent 按 skill 完整修订并再次确认 |
| Task 长期排队 | 查询 Task 状态；超出服务方给出的预期后联系服务方 |

联系服务方时附上 Kit 版本、所选 harness、发生时间、失败步骤和完整错误文本，但不要发送
无关的本地敏感信息。

## 安全边界

- 只连接服务方发放的 HTTPS 地址，并只使用对应 CA；不得关闭或绕过 TLS 验证。
- CA 证书只需只读挂载。Client Kit 不需要 TLS 私钥、Worker key 或签名私钥。
- 不要修改通过 hash 校验的 Kit，也不要从未批准来源替换 wheel、skill 或外部插件。
- Client 容器只安装 client wheel；不需要调度器服务端包、源码或服务端配置。
- 配置和 skill 只放在本次受信任的 workspace；使用任务所需的最小文件与网络权限。
- 遇到服务端状态、网络入口或 username 策略问题时联系服务方；本文不提供服务端操作。
