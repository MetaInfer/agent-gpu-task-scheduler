# Agent Client Kit 与纯客户端接入文档设计

## 1. 背景与决策

当前 `docs/submitting-from-an-agent-session.md` 同时面向两类读者：

1. 负责启动、配置和排障 Master/Worker 的服务提供方；
2. 只负责在 Docker 容器中运行 Claude Code、Codex CLI、pi 或 dsh 的 Agent Client 操作者。

混合两种角色导致客户端文档出现服务端源码路径、runtime profile、Master/Worker 启动命令、
qualification fixture 和内部 onboarding 生成器。客户端不仅不需要这些信息，也不应通过接入材料获得
服务端源码。

现有 Python distribution `agent-gpu-task-scheduler` 也不能作为客户端安装包：它构建的是整个
`src/agent_scheduler`，包含 Master、Worker、Scheduler、Proposal 处理和运行时身份实现。普通纯
Python wheel 会携带 `.py` 文件，因此「把现有 wheel 发给客户端但不开放 Git 仓库」不满足服务端
源码隔离要求。

本设计采用以下决策：

- 当前交付方案采用 **A：版本化 Agent Client Kit**；
- 新建独立的 client-only Python distribution；
- 服务方只向客户端交付 client wheel、依赖 wheelhouse、skill、配置模板、纯客户端文档和完整性
  manifest；
- 服务端源码和服务端 wheel 不进入 Client Kit；
- 方案 C（服务方托管远程 Streamable HTTP MCP）作为后续目标架构，本阶段不实现；
- 本文是设计 spec，本阶段不修改生产实现。

## 2. 目标

实施完成后，应满足以下目标：

1. Agent Client 无需 checkout、挂载或读取调度器 Git 仓库；
2. Agent Client 安装的 wheel 不包含任何服务端 Python package；
3. Agent Client 可以从 wheelhouse 离线安装 MCP Adapter；
4. Claude Code、Codex CLI、pi 和 dsh 使用同一 MCP 工具实现与同一 skill；
5. 四种 harness 的配置最终都启动同一个 client console entrypoint；
6. 客户端文档只包含客户端可执行的安装、配置、验证、触发和排障操作；
7. 服务端 qualification fixture 与对外 wheel 使用同一份工具/schema/REST client 实现，避免
   产品实现与测试夹具漂移；
8. 自动化测试可证明 client wheel 在没有服务端 package、源码目录和 `PYTHONPATH` 的环境中运行；
9. 后续切换到远程 MCP 时，skill、工具契约和 Proposal 流程保持不变。

## 3. 非目标

本阶段明确不做以下事项：

- 不实现远程 Streamable HTTP MCP Gateway；
- 不修改 Master 的监听地址、TLS 证书生成或网络拓扑；
- 不把 `X-Username` 升级为真实认证；
- 不增加 bearer token、OAuth 或 mTLS；
- 不改变现有 12 个 Submitter MCP 工具；
- 不改变 Proposal、Review、Compilation 或 Task 语义；
- 不向客户端交付服务端 REST/运维文档；
- 不向客户端交付 `agent-gpu-task-scheduler` 服务端 wheel；
- 不重新分发 Claude Code、Codex CLI、pi 或 dsh 本体；
- 默认不重新分发 `pi-mcp-adapter`、`dsh-mcp-bridge` 或其他第三方 npm package；
- 不修改客户端用户级 dotfile；
- 不增加交互式安装 wizard；
- 不承诺隐藏 client Adapter 自身的 Python 源码；只承诺隔离服务端源码；
- 不把完整 Proposal template 复制到客户端操作文档；
- 不在客户端文档中提供 Master/Worker 修复方法。

## 4. 信任边界与部署前提

### 4.1 参与方

**服务提供方**拥有并运行：

- Master REST 控制面；
- Worker；
- Processor/Reviewer；
- Ground Truth；
- runtime identity、Worker API key 和签名私钥；
- 服务端源码及服务端 wheel；
- Client Kit 构建和发布流程。

**Agent Client** 运行在 Docker 容器中，只拥有：

- 一个 Agent harness；
- Agent harness 自身的模型凭据；
- 解压后的 Client Kit；
- client-only Python venv；
- 客户端 workspace；
- 可读取的 TLS CA 文件；
- 服务方发放的 Master URL 和 username。

### 4.2 客户端输入

服务方必须向客户端提供以下四个部署值：

| 名称 | 含义 | 是否进入通用 Kit |
| --- | --- | --- |
| `MASTER_URL` | 客户端可达的 Master HTTPS base URL | 否 |
| `USERNAME` | 服务方已允许的 Submitter username | 否 |
| `CA_FILE` | 用于验证 `MASTER_URL` 的 CA/certificate 文件 | 否 |
| `CLIENT_WORKSPACE` | Agent 启动并发现 project-local skill 的目录 | 否 |

示例容器布局可以使用：

```text
/opt/agent-client/
├── kit/
└── venv/

/workspace/
├── .agents/skills/submit-gpu-task/
└── .claude/skills/submit-gpu-task -> ../../.agents/skills/submit-gpu-task

/shared/agent-scheduler-mvp/
└── tls/certificate.pem
```

路径只是文档示例，不是 Python package 的默认值。生产配置必须使用服务方实际发放的绝对路径。

### 4.3 TLS 前提

本设计不修复当前 localhost-only development certificate。服务方必须在交付前保证：

- Docker 网络能访问 `MASTER_URL`；
- certificate SAN 与 `MASTER_URL` 中的 host 匹配；
- `CA_FILE` 能验证该 certificate；
- CA 文件以只读方式进入容器。

客户端文档不得建议 `curl -k`、`verify=False` 或任何其他 TLS 绕过方法。发生 hostname mismatch
或证书链错误时，客户端只应核对获发参数并联系服务方。

即使共享 state root 对容器可达，client package 也只读取显式传入的 `CA_FILE`，不扫描或加载
state root 中的其他文件。推荐只读挂载单个 certificate 或 `tls/` 目录，而不是扩大 runtime 数据的
可见范围。

### 4.4 当前身份边界

本阶段继续沿用服务端已有的 `X-Username` 声明。`USERNAME` 不是 secret，client Adapter 会把它
固定到所有 REST 请求。MCP 工具参数中不得出现 username，Agent 不能在单次工具调用中切换身份。

远程网络暴露和真实认证属于方案 C 的前置工作，不纳入本设计的实现范围。

## 5. 对外交付物

### 5.1 Client-only Python distribution

新增独立 distribution：

| 项目 | 值 |
| --- | --- |
| Distribution name | `agent-gpu-task-scheduler-client` |
| Import package | `agent_scheduler_client` |
| Console entrypoint | `agent-scheduler-submitter` |
| Python floor | `>=3.10` |
| Runtime dependency | 仅 client 实际使用的 `httpx` 及其传递依赖 |

console entrypoint 的稳定调用契约为：

```bash
agent-scheduler-submitter \
  --base-url "$MASTER_URL" \
  --username "$USERNAME" \
  --ca-file "$CA_FILE"
```

三个参数均为必填；不从服务端 `Settings`、`AGENT_SCHEDULER_STATE_ROOT` 或源码目录推导。

### 5.2 版本化 Client Kit

每个 release 生成：

```text
agent-client-kit-<kit-version>/
├── wheels/
│   ├── agent_gpu_task_scheduler_client-<client-version>-py3-none-any.whl
│   └── <pinned transitive dependency wheels>
├── skills/
│   └── submit-gpu-task/
│       ├── SKILL.md
│       └── reference/proposal-template.md
├── config/
│   ├── mcp.example.json
│   ├── codex-mcp.example.toml
│   └── dsh-mcp.example.patch.yml
├── docs/
│   └── submitting-from-an-agent-client.md
├── MANIFEST.json
└── SHA256SUMS
```

Kit 中不得出现：

- `agent_scheduler/**`；
- 服务端 wheel；
- `src/`、`tests/`、`prompts/` 或 `.git/`；
- runtime secrets；
- 服务端配置；
- 指向私有仓库的 symlink；
- `/public/share/fh/agent-gpu-task-scheduler` 等仓库绝对路径（Proposal launcher 契约中必需的 Worker
  容器路径除外）。

### 5.3 第三方 Agent bridge

Claude Code 和 Codex CLI 原生支持 stdio MCP，不需要额外 bridge。

pi 和 dsh 的前置依赖保持外部责任：

- pi：`pi-mcp-adapter`；
- dsh：`dsh-mcp-bridge` 安装流程最终加载官方 `@deepseek-ai/dsh-mcp-client`，或客户端镜像直接
  配置同一官方 client。

Client Kit 的 manifest 记录实际通过验证的版本，但默认不复制这些 npm package。客户端文档只允许
两种前提：依赖已预装在镜像，或从客户批准的软件源安装。若未来决定离线重新分发 npm tarball，必须
先单独完成许可证和供应链评审，不把它隐式并入本设计。

## 6. 源码与 package 结构

### 6.1 唯一 client 实现

建议目录：

```text
packages/client/
├── pyproject.toml
└── src/agent_scheduler_client/
    ├── __init__.py
    ├── __main__.py
    ├── cli.py
    ├── mcp.py
    ├── rest.py
    └── tools.py
```

各文件职责：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 暴露 client version 和稳定公共类型 |
| `__main__.py` | 调用 `cli.main()`，支持 `python3 -m agent_scheduler_client` |
| `cli.py` | 参数校验、stdio server 生命周期、stderr diagnostics |
| `mcp.py` | MCP JSON-RPC framing、method dispatch、tool result/error envelope |
| `rest.py` | Master REST 调用、TLS、headers、轮询和结构化错误转发 |
| `tools.py` | 12 个工具常量、描述和 input schema |

`mcp.py` 不知道 state root；`rest.py` 不知道 Agent harness；`tools.py` 不发网络请求；`cli.py` 不包含
工具业务逻辑。

### 6.2 服务端 wheel 与 client wheel 的构建边界

同一份 `agent_scheduler_client` 源码用于两种构建目标：

- 服务端 distribution 的 wheel 包含 `agent_scheduler` 与 `agent_scheduler_client`，供内部 CLI 和
  qualification fixture 使用；
- client distribution 的 wheel 只包含 `agent_scheduler_client`。

根项目的 Hatch wheel target 因此显式包含：

```text
src/agent_scheduler
packages/client/src/agent_scheduler_client
```

`packages/client/pyproject.toml` 的 wheel target 只包含：

```text
src/agent_scheduler_client
```

两种 distribution 不应安装在同一个环境，因为它们会同时拥有 `agent_scheduler_client` 文件。服务端
环境安装服务端 wheel；客户端环境只安装 client wheel。

这一定义允许一份源码服务两个发布目标，同时提供可执行的 wheel 内容隔离证明。

### 6.3 现有内部 CLI 兼容

现有：

```bash
python3 -m agent_scheduler.cli.main mcp \
  --base-url <url> \
  --username <username>
```

暂时保留。服务端 wrapper 继续通过 `AGENT_SCHEDULER_STATE_ROOT` 找到 certificate，然后调用
`agent_scheduler_client` 的公开运行入口。对外文档和 Client Kit 配置绝不使用该 wrapper。

兼容 wrapper 的目的仅是避免一次性破坏内部 qualification、测试和运维材料；新的产品入口只有：

```text
agent-scheduler-submitter
```

### 6.4 从当前实现迁移

当前 `src/agent_scheduler/adapters/mcp.py` 中以下内容迁到 client package：

- `SubmitterMCPAdapter` 的 REST 操作；
- `_TOOLS` 及 schema helpers；
- JSON-RPC stdio loop；
- `_raise_for_status()` 与响应校验；
- tool argument 校验；
- Task/Event polling。

迁移后原路径不保留第二份实现。内部 import 全部指向 `agent_scheduler_client`，防止 schema 和错误
语义漂移。

## 7. Client CLI 契约

### 7.1 参数

```text
--base-url HTTPS_URL   required
--username USERNAME    required
--ca-file PATH         required
```

校验规则：

- URL scheme 必须是 `https`；
- URL 必须包含 host，且不接受 query、fragment 或 userinfo；
- username 必须匹配服务端模型已有的 `[A-Za-z0-9_.-]{1,64}`；
- CA path 必须是可读普通文件；
- 不提供 `--insecure`；
- 不以当前工作目录推导任何参数。

### 7.2 stdio 纪律

- stdout 只写一行一个 JSON-RPC response；
- 日志、warning 和 fatal diagnostics 只写 stderr；
- 无输入时不主动打印 banner；
- 忽略空白输入行；
- 收到 EOF 后关闭 HTTP client 并正常退出；
- 收到 SIGTERM 时关闭 HTTP client，不向 stdout 写非协议文本。

### 7.3 网络与错误

- TLS verification 始终使用 `--ca-file`；
- connect timeout 为 10 秒；
-普通 REST read timeout 保持足以覆盖服务端操作；
- `wait_for_task` 与 `wait_for_events` 的单次工具等待范围为 1–30 秒；
- REST 错误向 Agent 保留 HTTP status、`error_code` 和 `message`；
- 非 JSON 错误正文最多转发 500 个字符；
- mutating request 不做隐式应用层重试；幂等控制继续由 Agent 提供的 idempotency key 负责；
- `CHANGES_REQUESTED` 保持正常业务冲突，不改写为“网络故障”。

## 8. MCP 工具与数据流

### 8.1 工具面

工具维持现有 12 个：

```text
create_proposal
reply
confirm_revision
get_reviews
resume
cancel
get_proposal
get_task
cancel_task
wait_for_task
wait_for_events
get_logs
```

server name 统一为 `submitter`。工具输入中不增加 username、base URL、CA path 或任何运行时凭据。

为了让 Agent 能填写与固定 `X-Username` 一致的 Proposal Identity，`tools.py` 根据 CLI 已校验的
username 构造 tool list，并在 `create_proposal` 描述中明确当前 configured submitter username。
username 不是 secret；它只进入工具描述，不进入工具 input schema。Proposal template 使用
`<submitter-username>`，不再硬编码 `zz_chentian`。这样 Agent 无需读取进程 argv、环境变量或 MCP
配置文件，也不需要新增第 13 个 identity 工具。

### 8.2 数据流

```text
用户自然语言
    │
    ▼
Agent 读取本地 submit-gpu-task skill
    │
    ▼
Agent 调用 submitter MCP 工具（各 harness 可使用不同展示前缀）
    │ stdio JSON-RPC
    ▼
agent-scheduler-submitter
    │ HTTPS REST + 固定 X-Username
    ▼
MASTER_URL
    │
    ▼
Proposal review → Task → terminal state
```

客户端与服务端之间只传输：

- Proposal Markdown；
- proposal/task/log 标识和查询参数；
- 固定 `X-Username`；
- idempotency key；
- REST 成功响应或结构化错误。

client package 不读取 Ground Truth，不加载 Worker API key、Ed25519 key 或 TLS private key，也不直接
修改共享文件。

## 9. Skill 交付与发现

### 9.1 Canonical skill

仓库中的 `.agents/skills/submit-gpu-task/` 继续作为唯一 skill 源，Kit 构建时复制为普通文件，不保留
指向仓库的 symlink。

当前 `SKILL.md` 中要求用户启动 `agent-scheduler serve` 和 `agent-scheduler worker` 的内容不符合新
边界，实施时改为：

- 工具调用连接失败时立即停止；
- 报告连接错误；
- 建议客户端操作者核对获发的 endpoint/CA/config，并联系服务方；
- 不要求 Agent 或客户端操作者启动 Master/Worker；
- 不循环重试。

Skill 还需要消除两项 harness/deployment 绑定：

- 不再写死 `mcp__submitter__*` 这一种展示前缀，而是按 `create_proposal`、`reply`、
  `confirm_revision` 等语义名称引用 submitter MCP 工具；
- Proposal template 的 Identity 使用 `<submitter-username>`；Agent 从 `create_proposal` 工具描述中
  读取当前 configured username 并替换它，禁止猜测或沿用历史 username。

Proposal template 中的 launcher、container、mount 和 artifact path 属于 Agent 编写合法 Proposal
所需的产品契约，不是服务端运维步骤，因此继续随 skill 交付。

### 9.2 安装位置

客户端把 Kit 中的 skill 复制到：

```text
<CLIENT_WORKSPACE>/.agents/skills/submit-gpu-task/
```

四端发现机制：

| Agent | 机制 |
| --- | --- |
| Codex CLI | 原生读取 project-local `.agents/skills/` |
| pi | 原生读取 project-local `.agents/skills/` |
| Claude Code | `.claude/skills/submit-gpu-task` 相对 symlink 指向 canonical skill |
| dsh | patch 中 `customSkillDirs` 指向 `<CLIENT_WORKSPACE>/.agents/skills` |

项目级安装优先于用户级安装，以便 Kit 版本同时约束 wheel 与 skill。文档提醒客户端移除或避免启用
同名旧版全局 skill；自动化测试校验 Kit 中 skill hash 与 manifest 一致。

## 10. 四端配置模板

### 10.1 模板变量

配置模板使用明确的不可混淆 token：

```text
@@CLIENT_ENTRYPOINT@@
@@MASTER_URL@@
@@USERNAME@@
@@CA_FILE@@
@@CLIENT_WORKSPACE@@
```

客户端文档要求在使用前替换所有 token，并提供一条检查命令确保配置中不再存在 `@@...@@`。

`@@CLIENT_ENTRYPOINT@@` 必须替换为 venv 中 console script 的绝对路径，例如：

```text
/opt/agent-client/venv/bin/agent-scheduler-submitter
```

### 10.2 Claude Code 与 pi

`config/mcp.example.json` 是两者共用的 MCP server 声明。它包含：

- `command`：client entrypoint 绝对路径；
- `args`：`--base-url`、`--username`、`--ca-file`；
- `cwd`：client workspace；
- `directTools`：12 个工具名。

pi 使用 `directTools` 将工具提升为原生形态；Claude Code 必须通过契约测试证明忽略该扩展字段。

Claude Code 推荐一次性运行：

```text
--strict-mcp-config --mcp-config <rendered-config>
```

不写用户级 MCP 配置。

pi 使用 `pi-mcp-adapter` 提供的显式 MCP config 参数或隔离的 project-local 配置，不修改用户级默认
配置。pi provider/model 仍由客户端自身选择，Client Kit 不携带模型凭据。

### 10.3 Codex CLI

`config/codex-mcp.example.toml` 提供可读、可解析的参考配置；客户端文档推荐将相同键以单次 `-c`
override 传入当前 Codex 进程，从而不写 `~/.codex/config.toml`。

配置必须包含：

- command；
-完整 args；
- client workspace cwd。

不再设置 `AGENT_SCHEDULER_STATE_ROOT`，也不依赖仓库 cwd。

### 10.4 dsh

`config/dsh-mcp.example.patch.yml` 包含两个 `insert` entry：

1. `@deepseek-ai/dsh-mcp-client` 的 stdio Submitter server；
2. `@deepseek-ai/dsh-skill-filesystem` 的 project-local skill root。

MCP entry 使用 client entrypoint、完整 args 和 client workspace cwd。客户端通过单次 `--patch`
加载，不修改 profile 的持久 patch。

## 11. 纯客户端文档

### 11.1 新文档

新增：

```text
docs/submitting-from-an-agent-client.md
```

Kit 构建时复制到：

```text
docs/submitting-from-an-agent-client.md
```

文档必须能够脱离仓库上下文独立阅读。

### 11.2 章节顺序

1. **读者与边界**：只面向 Agent Client；不需要源码，不负责 Master/Worker；
2. **你应该已经收到什么**：Kit、`MASTER_URL`、`USERNAME`、`CA_FILE`、workspace；
3. **容器前置条件**：Python 版本、Agent CLI、自身模型认证、pi/dsh bridge；
4. **校验 Client Kit**：`sha256sum -c SHA256SUMS`；
5. **离线安装 wheel**：建立 venv，使用 `--no-index --find-links`；
6. **安装 skill**：复制 canonical skill，建立 Claude symlink；
7. **填写公共参数**：只使用服务方发放值；
8. **配置 MCP**：Claude/Codex/pi/dsh 四选一；
9. **分层预检**：文件、TLS/network、MCP、harness；
10. **触发任务**：自然语言示例；
11. **会话内部流程**：create/confirm/revise/wait/report；
12. **客户端排障**：只包含客户端能执行的动作；
13. **安全边界**：只读 CA、不得绕过 TLS、不得安装服务端 wheel。

### 11.3 安装命令

living client 文档使用直接 `python3` 命令，不使用 `uv`。离线安装的规范形态为：

```bash
python3 -m venv /opt/agent-client/venv
/opt/agent-client/venv/bin/python3 -m pip install \
  --no-index \
  --find-links /opt/agent-client/kit/wheels \
  "agent-gpu-task-scheduler-client==<kit-version>"
```

`<kit-version>` 在实际 release 文档中由 Kit 版本替换或从 manifest 读取；它不是让客户端猜测的值。

### 11.4 分层预检

预检从无副作用到有网络访问：

1. **Artifact**：hash、entrypoint、skill、CA 文件；
2. **HTTPS**：

   ```bash
   curl --cacert "$CA_FILE" "$MASTER_URL/health"
   ```

3. **本地 MCP**：向 entrypoint 发送 `initialize` 和 `tools/list`，应得到 12 个工具；
4. **Harness**：确认 `submitter` connected、skill 被发现。

文档明确：MCP `initialize`/`tools/list` 由本地 Adapter 回答，不访问 REST；所以“connected”不能替代
HTTPS preflight。

### 11.5 触发与预期流程

客户端只需使用自然语言，例如：

```text
用 4 张卡提交一个 GPU 任务，并等待最终结果。
```

文档只概述：

```text
create_proposal
→ confirm_revision
→ 必要时 get_reviews/reply/confirm_revision
→ wait_for_task
→ 报告终态
```

完整 15 节契约只保存在版本化 skill/template 中，避免文档复制后漂移。

### 11.6 客户端排障边界

| 现象 | 客户端动作 |
| --- | --- |
| wheel 安装失败 | 检查 Python 版本、hash 和 wheelhouse 完整性 |
| entrypoint 不存在 | 检查 venv 路径和安装结果 |
| MCP process failed | 检查 command 绝对路径、args 和 cwd |
| CA 不可读 | 检查容器只读挂载与文件权限 |
| TLS hostname mismatch | 核对获发 URL/CA 并联系服务方；不得绕过验证 |
| 连接拒绝、timeout 或 health 失败 | 联系服务方 |
| `403 USERNAME_NOT_ALLOWED` | 核对获发 username，然后联系服务方 |
| `422 INVALID_PROPOSAL` | 确认 skill 与 Kit 同版本，读取服务端 message |
| `CHANGES_REQUESTED` | 正常评审流程，让 Agent 按 skill 完整修订 |
| Task 长期排队 | 查询 Task 状态；必要时联系服务方 |
| Agent harness 未认证 | 完成该 harness 自身的模型登录，与 MCP 配置分开处理 |

客户端文档不得包含：

- `serve`、`worker`、`init-runtime`、`reload-users`；
- `ss -lntp` 等服务端诊断；
- harness/profile mode；
- Processor/Reviewer 模型 token；
- qualification fixture；
- `build_onboarding()`；
- `python3 -m agent_scheduler.cli.main`；
- 服务端仓库 cwd 或源码 checkout；
- Worker API key、签名私钥或 TLS private key 路径。

### 11.7 旧文档定位

在 `docs/submitting-from-an-agent-session.md` 顶部增加 audience 提示：

> 本文用于服务提供方内部部署与端到端联调。只运行 Agent Client、且没有服务端源码访问权限的
> 读者，请使用 `submitting-from-an-agent-client.md`。

旧文档继续保留内部操作，不把它作为 Client Kit 的组成部分。

## 12. Kit 构建与发布

### 12.1 构建输入

Kit builder 只接受显式输入：

- 已构建的 client wheel；
- 完整 dependency wheelhouse；
- canonical skill directory；
-三份配置模板；
-纯客户端文档；
- kit/client version；
-测试通过的 harness version map。

它不得从当前 Python environment 隐式复制任意 package，也不得递归打包仓库根。

### 12.2 Manifest

`MANIFEST.json` 使用稳定、可测试的结构：

```json
{
  "kit_version": "0.2.0",
  "client": {
    "distribution": "agent-gpu-task-scheduler-client",
    "version": "0.2.0",
    "wheel": "wheels/agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl",
    "python_requires": ">=3.10"
  },
  "dependencies": [
    {
      "distribution": "httpx",
      "version": "0.28.1",
      "wheel": "wheels/httpx-0.28.1-py3-none-any.whl"
    }
  ],
  "master_api": "v1",
  "mcp_server_name": "submitter",
  "tool_count": 12,
  "tested_harnesses": {
    "claude": "2.1.247",
    "codex": "0.149.1",
    "pi": "0.84.3",
    "dsh": "0.1.1-rc.2"
  }
}
```

`dependencies` 是一个数组，每个元素对应一个随 Kit 打包的第三方依赖 wheel（`distribution`
是标准化、去重后的 distribution 名称，`wheel` 是相对 Kit 根的路径，均以 `wheels/<file>.whl`
形式给出）；client distribution 本身不出现在这个数组里。加这个字段是因为 wheelhouse 必须能
在完全离线、没有任何 resolver 的环境下被校验和安装——manifest 需要显式声明每个依赖 wheel
的身份，验证器才能在不解析依赖图的前提下确认每个 wheel 都是预期的那一个。

上述 harness version 是本设计时环境中实际检测到的基线；实施时若验证环境已升级，manifest 写入
实际通过 T5 的版本，不伪造旧版本结果。

### 12.3 Hashes

`SHA256SUMS` 覆盖 Kit 中除自身外的每个普通文件。构建结果按相对路径排序，保证输出稳定。发布前：

1. 从空目录验证 `sha256sum -c SHA256SUMS`；
2. 验证 manifest 中声明的 wheel 存在；
3. 验证没有 symlink、socket、device 或其他特殊文件；
4. 验证 archive 解压后路径不会逃逸 Kit 根目录。

### 12.4 版本规则

Kit version 等于 client distribution version。以下任意变化都发布新 Kit version：

- MCP tool/schema；
- REST client 行为；
- CLI 参数或错误语义；
- skill/template；
-配置模板；
-纯客户端文档。

Master API 保持 `/api/v1`。Kit manifest 记录兼容 API major；服务方只向客户端发放与当前 Master
兼容的 Kit。

### 12.5 发布渠道

Kit 通过私有 artifact registry 或只读共享发布目录交付。客户端不需要访问 Git remote。发布物可以
额外压缩为 tar archive，但 archive 只是传输容器；解压后的 manifest 与 hashes 才是内容权威。

## 13. 测试设计

### 13.1 T1：wheel 源码隔离门禁

构建 client wheel 后读取 zip member list，断言：

- 存在 `agent_scheduler_client/**`；
- 不存在 `agent_scheduler/**`；
- 不存在 server modules、prompts、tests、config 或 docs；
- metadata 中 distribution name 和 Python floor 正确；
- console entrypoint 指向 `agent_scheduler_client.cli:main`；
- wheel 文本文件不含私有仓库绝对路径。

在全新 venv 中只安装 Client Kit wheelhouse，清空 `PYTHONPATH`，切换到空 workspace，断言：

```text
agent-scheduler-submitter --help     succeeds
import agent_scheduler_client        succeeds
import agent_scheduler               fails with ModuleNotFoundError
```

这是服务端源码隔离的权威自动化证据。

### 13.2 T2：client 单元契约

覆盖：

- CLI 三个必填参数；
- URL、username、CA file 校验；
- stdout/stderr 隔离；
- 12 个 tool 名称、描述、schema；
- `create_proposal` 工具描述包含 CLI 配置的 username，input schema 不包含 username；
- tool argument 类型校验；
- REST method/path/body/headers；
- idempotency header；
- CA file 传给 HTTP client；
-结构化与非结构化错误正文；
- wait timeout clamp；
- Task terminal states；
- EOF 后 client close。

### 13.3 T3：Client Kit 契约

覆盖：

- 目录结构完整；
- manifest schema 与实际 artifact 一致，含 `dependencies` 数组与每个依赖 wheel 的
  distribution/version 元数据；
- `SHA256SUMS` 覆盖全部普通文件；
- wheelhouse 在禁止网络时可安装；
- skill frontmatter 有效；
- template 相对引用存在；
- template 使用 `<submitter-username>`，不含部署 username literal；
- skill 按语义工具名工作，不绑定某一种 harness 展示前缀；
- skill 不再要求客户端启动 Master/Worker；
- JSON/TOML/YAML 配置可解析；
- 所有 template token 有定义；
- 四端最终执行同一 console entrypoint；
- 配置不含服务端 module path、state-root env、仓库 cwd 或 `PYTHONPATH`；
- Kit 不含 symlink 和服务端源码。

### 13.4 T4：无源码通路集成

测试建立隔离临时环境：

1. 构建 client wheel 与 wheelhouse；
2. 创建空 venv；
3. 安装 client distribution；
4. 清空 `PYTHONPATH`；
5. cwd 指向不在仓库内的临时 workspace；
6. 启动测试 Master；
7. 使用测试 CA 启动 `agent-scheduler-submitter`；
8. 发送 MCP initialize、tools/list 和 create_proposal；
9. 断言 Proposal 由 REST 控制面创建；
10. 断言 client process 无法 import `agent_scheduler`。

测试 Master 属于测试 fixture，不进入 Kit，也不写进客户端文档。

### 13.5 T5：四端 opt-in 验证

Claude/Codex/pi/dsh 的真实 connectivity 与 qualification 测试改为从 Client Kit 组装运行环境。每个
harness 的 argv、cwd、env 和 prompt 均不得出现服务端仓库路径。

T5 至少验证：

- MCP server 可被 harness 启动；
- 12 个工具可达；
- project-local skill 可发现；
- 一个测试 Proposal 能通过 MCP 创建；
-真实 GPU qualification 仍能从 Ground Truth 验证，不信任 Agent 自述。

真实 harness、模型和 GPU 费用继续由显式环境变量 opt-in，不进入默认测试门禁。

### 13.6 Living-doc 门禁

客户端文档测试至少禁止以下字符串：

```text
/public/share/fh/agent-gpu-task-scheduler
python3 -m agent_scheduler.cli.main
build_onboarding
AGENT_SCHEDULER_HARNESS_MODE
ANTHROPIC_AUTH_TOKEN
reload-users
init-runtime
```

同时断言文档包含：

- `python3 -m venv`；
- `--no-index` 与 `--find-links`；
- `sha256sum -c`；
- `submit-gpu-task`；
- `--ca-file`；
- Claude Code、Codex CLI、pi、dsh；
- 12 工具预检；
- TLS 不得绕过；
- 服务端故障联系服务方。

禁词测试只约束 living client doc，不扫描历史 design/plan 文档。

## 14. 预计实施文件

未来 implementation plan 应覆盖以下文件集合；具体任务顺序由 plan 定义。

### 新建

```text
packages/client/pyproject.toml
packages/client/src/agent_scheduler_client/__init__.py
packages/client/src/agent_scheduler_client/__main__.py
packages/client/src/agent_scheduler_client/cli.py
packages/client/src/agent_scheduler_client/mcp.py
packages/client/src/agent_scheduler_client/rest.py
packages/client/src/agent_scheduler_client/tools.py
scripts/build_client_kit.py
config/client/mcp.example.json
config/client/codex-mcp.example.toml
config/client/dsh-mcp.example.patch.yml
docs/submitting-from-an-agent-client.md
tests/test_client_package.py
tests/test_client_kit.py
```

测试是否拆成更多 focused files 由 implementation plan 根据现有测试结构决定，但不得把 package、Kit、
doc 和四端契约全部塞进一个超大测试文件。

### 修改

```text
pyproject.toml
src/agent_scheduler/cli/main.py
src/agent_scheduler/adapters/mcp.py
src/agent_scheduler/adapters/onboarding.py
src/agent_scheduler/adapters/submitter.py
.agents/skills/submit-gpu-task/SKILL.md
docs/submitting-from-an-agent-session.md
tests/test_cli.py
tests/test_api.py
tests/test_onboarding.py
tests/test_submitter_harness.py
tests/test_real_onboarding.py
tests/test_real_qualification.py
```

如果迁移后 `src/agent_scheduler/adapters/mcp.py` 只剩无价值的 re-export，应删除该文件并一次性更新
内部 import；不要长期保留空壳与第二套命名。内部 CLI compatibility wrapper 可直接 import client
package。

## 15. 方案 C 的可行性与保留 seam

### 15.1 结论

远程 MCP 技术上可行，四种 harness 均已有 Streamable HTTP 路径：

- Claude Code 原生 HTTP MCP，支持 headers 和 OAuth；
- Codex CLI 原生 Streamable HTTP，支持 bearer token env 和 OAuth；
- pi 通过第三方 `pi-mcp-adapter` 支持 Streamable HTTP；
- dsh 官方 `@deepseek-ai/dsh-mcp-client` 支持 `streamable-http`。

长期形态可以是服务方托管的 MCP Gateway：

```text
Agent harness
    │ Streamable HTTP + authenticated identity
    ▼
Submitter MCP Gateway
    │ trusted REST + server-mapped X-Username
    ▼
Master REST control plane
```

### 15.2 本阶段不采用 C 的原因

C 需要新增正式服务边界，而不是 transport flag：

-标准 Streamable HTTP server；
-正式 DNS/TLS；
- token/OAuth identity 到 username 的可信映射；
-禁止客户端控制或伪造 username；
- reverse proxy timeout、rate limit、audit、health 和 rollout；
-四端 MCP protocol revision compatibility；
-共享 Gateway 的高可用和 blast-radius 管理。

当前 Master 的 localhost certificate 和声明式 `X-Username` 不足以支撑生产远程 Gateway，所以 C
不能通过纯文档修改交付。

### 15.3 A 为 C 保留的接口

本设计通过以下方式降低未来迁移成本：

- tools/schema 位于 transport-independent `tools.py`；
- REST 调用位于 `rest.py`；
- stdio framing 单独位于 `mcp.py`；
- skill 只依赖 server name 和工具语义，不依赖本地进程路径；
-四端统一使用 server name `submitter`；
- Proposal 工作流不感知 transport；
- Kit config 与 skill 分目录，未来可以只替换 config。

未来 C 上线时，客户端主要配置变化应为：

```diff
- command + args: agent-scheduler-submitter ...
+ url: https://<service>/mcp
```

skill、Proposal template 和工具调用流程保持不变。C 的服务端实现应使用官方 MCP SDK，而不是把当前
手写 stdio JSON-RPC loop 直接扩展成 HTTP protocol implementation。

参考资料：

- <https://modelcontextprotocol.io/specification/draft/basic/transports>
- <https://modelcontextprotocol.io/specification/draft/basic/authorization>
- <https://github.com/modelcontextprotocol/python-sdk>
- <https://code.claude.com/docs/en/mcp>
- <https://developers.openai.com/codex/mcp>
- <https://github.com/nicobailon/pi-mcp-adapter>
- <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md>

## 16. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| client wheel 意外包含服务端 package | T1 直接检查 wheel member list，并在空 venv 断言 server import 失败 |
| skill 与 wheel 版本漂移 | 二者由同一个 Kit version 和 manifest/hash 绑定 |
| 服务端与 client 各维护一份 MCP schema | 迁移到唯一 `agent_scheduler_client` 源码，内部 fixture 同样导入它 |
| 配置仍引用仓库 cwd 或 module | template contract 和 T3 禁止这些值 |
| 客户端使用错误 CA 或 URL | 三参数必填、启动校验、HTTPS preflight、禁止 insecure fallback |
| localhost certificate 无法用于容器网络 | 作为服务方交付前提；本设计不伪装成客户端可修复问题 |
| 旧全局 skill 覆盖 Kit skill | 推荐 project-local 安装，manifest hash 校验，文档提醒移除同名旧版本 |
| pi/dsh bridge 供应链不可控 | 不默认重分发；记录验证版本；客户从获准渠道安装 |
| root wheel 与 client wheel 同时安装导致文件 ownership 冲突 | 明确两种 distribution 互斥；服务端只装 root wheel，客户端只装 client wheel |
| living doc 又混入服务端步骤 | audience 边界、禁词测试和客户端动作排障表 |
| Client Kit 构建递归带入仓库文件 | builder 使用显式 allowlist 输入，不打包仓库根 |

## 17. 验收标准

设计的未来实现只有在以下条件全部满足时才算完成：

1. `agent-gpu-task-scheduler-client` wheel 可独立构建；
2. wheel member list 不含 `agent_scheduler/**` 或服务端资源；
3. 空 venv 离线安装成功；
4. 空 workspace 中 `agent_scheduler` import 失败而 client entrypoint 工作；
5. client CLI 必须使用显式 HTTPS URL、username 和 CA file；
6. 12 个 MCP 工具与当前产品契约一致，且 Agent 能从工具描述获得 configured username；
7. Proposal template 不硬编码部署 username 或某一种 harness 工具前缀；
8. Client Kit manifest/hash 可完全验证；
9. Kit 中没有服务端 wheel、源码目录、runtime secret 或私有仓库 symlink；
10. canonical skill 不再指示客户端启动 Master/Worker；
11. 新文档覆盖四种 harness 且只包含客户端操作；
12. 旧文档明确标注为服务方内部文档；
13. 四端配置不写用户级 dotfile，也不引用源码 cwd；
14. 无源码通路测试成功创建 Proposal；
15. 默认测试套件通过；
16. opt-in 四端 connectivity/qualification 在获准环境中可运行；
17. 方案 C 被明确记录为后续工作，而不是在客户端文档中冒充已支持功能。
