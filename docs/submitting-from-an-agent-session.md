# 从一个 Agent 会话提交 Proposal

> **读者范围：服务提供方内部部署与端到端联调。** 如果你只运行 Agent Client、没有服务端
> 源码访问权限，请使用 [纯客户端接入文档](submitting-from-an-agent-client.md)。

回答一个具体问题：手上有一个 Agent 会话——Claude ​Code、Codex CLI、pi 或 dsh 之一——
怎么让它给调度器发 Proposal？四家的答案形状相同，细节不同，本文档逐一给出。

**简短答案**：MCP 和 skill **都是必须的**，作用不同，缺一不可。

| | 作用 | 必要性 | 不装的后果 |
| --- | --- | --- | --- |
| MCP Server | **通路** — 让会话能调用控制面 | 必须 | 会话根本没有可用的工具，只能让你手动跑 curl |
| Skill | **知识** — 告诉它 Proposal 怎么写、评审被拒怎么办 | **必须** | 工具能调，但内容几乎必然被 `422` 拒绝 |

第二行不是理论。首次真实资格运行就是这么失败的：会话自己发明了
`--nproc-per-node`、`--output`、`--log` 四个 flag，而冻结 launcher 只收两个位置参数，
连续 7 次被控制面拒绝。它不知道契约，就一定会猜——这条教训不因换了哪家 Agent 而失效：
装了 MCP 不装 skill，等于把猜测的自由留给了会话本身。

本仓库已经把两样都准备好了：

- MCP 配置：本节按四家分别给出，内容与内部生成器 `build_onboarding()` 的实际输出一致
  （`config/submitter-mcp.example.json` 仍保留，供 Claude ​Code 与 pi 直接复用）
- Skill：`.agents/skills/submit-gpu-task/`（含真实通过评审的 Proposal 模板），四家各有
  自己的发现方式，见步骤 3

---

## 步骤 1 · 先把 Master 和 Worker 起起来

MCP Adapter 只是个翻译层，后面必须有活着的控制面。

```bash
cd /public/share/fh/agent-gpu-task-scheduler
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=claude
export AGENT_SCHEDULER_WORKER_MODE=remote
export ANTHROPIC_AUTH_TOKEN=...        # 或 ANTHROPIC_API_KEY

python3 -m agent_scheduler.cli.main serve     # 终端 1
python3 -m agent_scheduler.cli.main worker    # 终端 2
```

确认：

```bash
curl -sk https://127.0.0.1:8443/health
```

`workers` 必须是 `1`，`integrity` 必须是 `valid`。**这一步不通过，后面全都白搭。**

> Master 起不来最常见的原因是端口被上一次没退干净的进程占着。`serve` 会打印
> `address already in use` 然后静默退出，而旧进程仍在服务旧代码——症状是「我明明改了却没生效」。
> 用 `ss -lntp | grep 8443` 确认。

`AGENT_SCHEDULER_HARNESS_MODE=claude` 意味着 Master 内部的 Processor 和 Reviewer 角色会真的
调用 Claude，**会产生费用**——这与你打算用哪家 Agent 来提交 Proposal 是两回事：Processor/
Reviewer 是控制面固定的内部角色，不随 Submitter 换成 Codex CLI/pi/dsh 而改变。只想跑通链路
不想花钱，把它设成 `fake`——除了 Processor/Reviewer 是假的，其余（签名编译、调度、租约、
Docker、产物校验）全是真的。

## 步骤 2 · 给会话装 MCP Server

四家最终都指向同一条命令，服务端没有任何 harness 专属分支：

```
python3 -m agent_scheduler.cli.main mcp --base-url https://127.0.0.1:8443 --username zz_chentian
```

`AGENT_SCHEDULER_STATE_ROOT` 必须与 Master 一致——Adapter 用
`<state-root>/tls/certificate.pem` 作 CA。区别只在于每家怎么把这条命令声明给会话。
下面每节的配置内容都以 `build_onboarding()` 的真实输出为准，可以自己核对：

```bash
python3 -c "
import sys
from pathlib import Path
from agent_scheduler.adapters.onboarding import build_onboarding
config = build_onboarding('claude', output_dir=Path('/tmp/demo'),
    project_root=Path('/public/share/fh/agent-gpu-task-scheduler'),
    state_root=Path('/public/share/agent-scheduler-mvp'),
    base_url='https://127.0.0.1:8443', username='zz_chentian',
    python_path=Path(sys.executable).resolve())
print(config.argv, config.files)
"
```

### Claude ​Code

一次性前置：无。

三选一。

**方式 A：`claude mcp add`（推荐，作用域可控）**

```bash
cd /public/share/fh/agent-gpu-task-scheduler

claude mcp add submitter \
  --scope project \
  --env AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp \
  -- python3 -m agent_scheduler.cli.main mcp \
       --base-url https://127.0.0.1:8443 \
       --username zz_chentian
```

`--scope project` 写进项目配置；换成 `user` 则对你所有会话生效。

**方式 B：手写 `.mcp.json`**

把 `config/submitter-mcp.example.json` 的内容复制到仓库根的 `.mcp.json`：

```bash
cp config/submitter-mcp.example.json .mcp.json
```

在这个目录里启动的 Claude 会话都会自动加载它。**注意这是全局副作用**——所以仓库里默认
只放 `.example`，不放生效的 `.mcp.json`，要不要开由你决定。

**方式 C：一次性会话**

```bash
claude --mcp-config config/submitter-mcp.example.json --strict-mcp-config
```

不落任何配置，适合试一次。生成器内部就是这个形状：`build_onboarding("claude", ...)`
把上面的 JSON 写到一个临时目录，再传 `--strict-mcp-config --mcp-config <path>`。

确认装上了：在会话里执行 `/mcp`，应该看到 `submitter` 处于 `connected`，并列出 12 个工具：

```
create_proposal   reply           confirm_revision   get_reviews
get_proposal      resume          cancel             get_task
cancel_task       wait_for_task   wait_for_events    get_logs
```

命令行确认用 `claude mcp list`，会显示 `✔ Connected`。

> **`Connected` 不代表整条链通。** Adapter 的 `initialize` 和 `tools/list` 是本地应答的，
> 不碰 REST——Master 没起也照样显示 `Connected`。真正的连通性以步骤 1 的 `/health` 为准。

### Codex CLI

一次性前置：无（但需要先 `codex login` 完成认证，见排障表）。

Codex 从 `$CODEX_HOME/config.toml` 加载配置，不支持在命令行内联整段 TOML。避免用
`codex mcp add`（会写进 `~/.codex/config.toml`，影响你所有项目），改成把配置渲染进
一个专用、临时的 `CODEX_HOME`：

```toml
# /tmp/agent-scheduler-codex-home-xyz/config.toml
[mcp_servers.submitter]
command = "/usr/bin/python3"
args = ["-m", "agent_scheduler.cli.main", "mcp",
        "--base-url", "https://127.0.0.1:8443", "--username", "zz_chentian"]
cwd = "/public/share/fh/agent-gpu-task-scheduler"

[mcp_servers.submitter.env]
AGENT_SCHEDULER_STATE_ROOT = "/public/share/agent-scheduler-mvp"
```

```bash
CODEX_HOME=/tmp/agent-scheduler-codex-home-xyz codex exec \
  --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
  -C /public/share/fh/agent-gpu-task-scheduler
```

`build_onboarding("codex", ...)` 生成的正是这样一份 `config.toml`（Client Kit 场景下
没有上面的 `env` 表，因为 client entrypoint 走 HTTPS，不需要 state root），并把它当作
`CODEX_HOME` 唯一的配置来源——不再有第二套从同样的值重新拼出来的 `-c` flag，Codex 进程
读到的字节和渲染出来的字节完全一致。如果本机之前 `codex login` 过，fixture 会把真实
`CODEX_HOME`（未设置时是 `~/.codex`）下的 `auth.json` 复制（不是移动或软链接）进这个
临时目录，登录态照常可用；用 API key 认证不受影响，因为那条路径不依赖 `CODEX_HOME`
文件内容。

### pi

一次性前置（做一次，对所有项目生效）：

```bash
pi install npm:pi-mcp-adapter
```

`pi-mcp-adapter` 直接读项目根的 `.mcp.json`，**与 Claude ​Code 共用同一份文件**——不需要
第二份配置。真正需要额外声明的是 `directTools`：默认情况下 adapter 会把 MCP 收敛成单个代理
工具 `mcp({tool: "create_proposal", args: {...}})` 来节省 context，12 个工具都要经它转述才能
调用；仍然可用，但多了一层，也多了一层出错机会。`build_onboarding("pi", ...)` 会直接把
12 个工具名列进 `directTools`，提升为原生形态：

```json
{
  "mcpServers": {
    "submitter": {
      "command": "/usr/bin/python3",
      "args": ["-m", "agent_scheduler.cli.main", "mcp",
                "--base-url", "https://127.0.0.1:8443",
                "--username", "zz_chentian"],
      "cwd": "/public/share/fh/agent-gpu-task-scheduler",
      "env": {"AGENT_SCHEDULER_STATE_ROOT": "/public/share/agent-scheduler-mvp"},
      "directTools": [
        "create_proposal", "reply", "confirm_revision", "get_reviews",
        "resume", "cancel", "get_proposal", "get_task", "cancel_task",
        "wait_for_task", "wait_for_events", "get_logs"
      ]
    }
  }
}
```

把这份内容写进仓库根的 `.mcp.json`（Claude ​Code 会忽略它不认识的 `directTools` 字段，
两家可以共用一份文件）。

pi 本身可能有默认 provider，但本项目的 T2/T3 fixture 不依赖这个隐藏默认值：
`AGENT_SCHEDULER_PI_PROVIDER` 和 `AGENT_SCHEDULER_PI_MODEL` 都是必填，缺任意一个或两个都没设
都会在启动前得到 `OnboardingError`。fixture 会把它们转换成显式 argv：

```bash
export AGENT_SCHEDULER_PI_PROVIDER=<your-provider>
export AGENT_SCHEDULER_PI_MODEL=<your-model>
pi --provider "$AGENT_SCHEDULER_PI_PROVIDER" --model "$AGENT_SCHEDULER_PI_MODEL"
```

### dsh

一次性前置（做一次，对所有 profile 生效）：

```bash
dsh plugin --profile headless add dsh-mcp-bridge
```

之后每轮用 `--patch <overlay>` 声明 server，不改 `$DSH_HOME/profiles/headless/cordis.patch.yml`
本身——`build_onboarding("dsh", ...)` 生成的 patch 内容如下（`insert` 指令，两个 cordis 条目）：

```yaml
# Generated per qualification run. Do not edit; regenerate instead.
- insert:
    - id: mcp-submitter
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: submitter
        transport: stdio
        command: "/usr/bin/python3"
        args: ["-m", "agent_scheduler.cli.main", "mcp", "--base-url", "https://127.0.0.1:8443", "--username", "zz_chentian"]
        cwd: "/public/share/fh/agent-gpu-task-scheduler"
        env:
          AGENT_SCHEDULER_STATE_ROOT: "/public/share/agent-scheduler-mvp"
    - id: skill-filesystem-submitter
      name: '@deepseek-ai/dsh-skill-filesystem'
      config:
        providerName: submitter
        customSkillDirs:
          - "/public/share/fh/agent-gpu-task-scheduler/.agents/skills"
```

```bash
dsh --profile headless --patch submitter-mcp.patch.yml
```

`dsh-mcp-bridge` 是第三方包；它安装的正是 `@deepseek-ai/dsh-mcp-client` 这个官方 MCP client
插件的示例 patch 条目——上面这份 patch 里 `name: '@deepseek-ai/dsh-mcp-client'` 的那一段，就是
照着 `dsh-mcp-bridge` 自带的示例配置抄出来的实测结构，**不是**换了一个替代实现。
如果不想依赖这个第三方包，完全可以跳过 `dsh plugin add`，直接把 `@deepseek-ai/dsh-mcp-client`
声明为依赖，自己写同样的 `insert` 条目——效果等价，因为最终生效的插件本来就是同一个。

### 确认装上了

不同 harness 的确认方式不同（`/mcp` 是 Claude ​Code 的命令，pi/codex/dsh 各有各的列出已连接
server 的方式），但原理相同：**列出的工具 connected 不代表整条链通。** Adapter 的
`initialize` 和 `tools/list` 是本地应答的，不碰 REST——Master 没起也照样显示已连接。
真正的连通性以步骤 1 的 `/health` 为准。

## 步骤 3 · 装 Skill

Skill 的规范位置是 `.agents/skills/submit-gpu-task/`（遵循
[Agent Skills 标准](https://agentskills.io/specification)），四家各自有原生或搭桥的发现方式，
都不需要你手工复制内容：

| Agent | 发现方式 |
| --- | --- |
| Codex CLI | 原生读取项目根 `.agents/skills/`，无需任何配置 |
| pi | 原生读取项目根 `.agents/skills/`（同上） |
| Claude ​Code | `.claude/skills/submit-gpu-task` 是指向 `.agents/skills/submit-gpu-task` 的 symlink，Claude 按自己惯例扫描 `.claude/skills/` 时会跟随它 |
| dsh | 没有原生 `.agents/skills` 扫描，靠步骤 2 里那份 patch 的 `skill-filesystem-submitter` 条目，把 `customSkillDirs` 指向 `.agents/skills` |

也就是说：dsh 的 skill 发现和 MCP 声明是同一份 patch 文件里的两个条目，装 MCP 的同时就把
skill 也接上了；其余三家装好 MCP 之后, skill 已经在原地，不用再多做一步。

想让 Claude ​Code 的 skill 对所有项目可用（而不只是这个仓库）：

```bash
cp -r .agents/skills/submit-gpu-task ~/.claude/skills/
```

用 `/skills`（Claude ​Code）或各家对应命令确认它出现在列表里。

## 步骤 4 · 触发

**不需要背特定关键词。** Skill 的 `description` 字段就是触发条件，会话会按语义匹配，四家
共用同一份 skill 内容，触发方式一致。自然说人话即可：

- 「跑一个 4 卡的 GPU 任务」
- 「提一个 8 卡的 all-reduce Proposal」
- 「submit a GPU task using 2 cards」

Claude ​Code 想强制触发可以打斜杠命令：

```
/submit-gpu-task 用 8 卡跑一次 all-reduce 和 GEMM 校验
```

其余三家没有 slash command 概念，直接把同样的意图写进初始 prompt 即可——只要它最终调用了
`create_proposal` 这个 MCP 工具，链路就通了。

---

## 会话内部会发生什么

装好之后，无论哪家 Agent，都会走这条路径（skill 里写死了）：

```
写 Proposal（15 节，完整）
   │
   ▼
create_proposal ──► 拿到 proposal_id + current_revision_id
   │
   ▼
confirm_revision（送独立 Reviewer）
   │
   ├─ 200 ────────────────────────► Task 已创建
   │
   └─ 409 CHANGES_REQUESTED
        │
        ▼
      get_reviews ─► reply（完整替换修订）─► confirm_revision（新幂等键）
                                                    │
                                                    ▼
                                              Task 已创建
   │
   ▼
wait_for_task 循环轮询（每次最多 30 秒）直到终态
   │
   ▼
COMPLETED / FAILED / CANCELLED / CLEANUP_FAILED / RECONCILIATION_REQUIRED
```

**被 `REQUEST_CHANGES` 是正常的，不是故障。** 上次真实运行里 4 个 Proposal 有 3 个被打回，
理由都是产物写入路径（`/data`）和校验路径（`/public/share`）看起来像两个不同文件；
补上 bind mount 声明后全部通过。这说明评审门是真在工作，与用哪家 Agent 提交无关——
Reviewer 是控制面的固定角色，不随 Submitter 换人而放水。

## 手动验证（不经过 Agent）

想确认通路本身没问题，可以直接喂 JSON-RPC 给 Adapter，跳过任何一家 Agent：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| python3 -m agent_scheduler.cli.main mcp --base-url https://127.0.0.1:8443 --username zz_chentian
```

能列出 12 个工具就说明 Adapter、TLS、控制面这条链是通的，问题只可能在 Agent 侧配置。

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Claude `/mcp` 里没有 `submitter` | 配置没被加载 | 确认启动目录，或改用 `--scope user` |
| `submitter` 显示 `failed` | Adapter 进程起不来 | 用上面的手动验证命令看真实报错 |
| `mcp requires --username` | 没传 `--username` | 加参数，或设 `AGENT_SCHEDULER_USERNAME` |
| 连接被拒 / TLS 失败 | Master 没起，或 `AGENT_SCHEDULER_STATE_ROOT` 不对 | Adapter 用 `<state-root>/tls/certificate.pem` 作 CA，路径必须和 Master 一致 |
| `403 USERNAME_NOT_ALLOWED` | 用户名不在白名单 | 用 `zz_chentian`，或 `reload-users` 加人 |
| `422 INVALID_PROPOSAL` | 内容不合契约 | 读 `message`。最常见是 argv 不是两个位置参数 |
| `409 IDEMPOTENCY_CONFLICT` | 同键不同负载 | 换新幂等键 |
| Task 长期 `BLOCKED` | 准入不满足 | 看观察界面 GPU 状态；容器严格串行，排队正常 |
| 权限不足读不到证书 | Submitter 账号不在 state-root 的属组里 | `stat -c '%G' <state-root>` 查看属组，把 Submitter 账号加进该组；证书路径是 `<state-root>/tls/certificate.pem`，不是 `secrets/` 下 |
| codex 报 `Not logged in` | codex CLI 本身未完成认证，与 MCP 配置无关 | 先 `codex login`（或设置对应模型厂商的 API key 环境变量），`codex login status` 确认正常后再重跑 |
| pi 启动即报错要求指定 provider | pi 默认 provider 是 `google`，没配这家凭据时直接拒绝启动 | 显式加 `--provider <name> --model <model>`，或在 `~/.pi/agent` 里配置默认 provider |
| dsh 卡住不动，像是在等待批准 | dsh 默认审批模式会对工具调用弹确认，headless 会话里没人来确认 | 设 `DSH_PERMISSION_MODE=danger-full-access`（或对应的 headless 免审批 flag）后启动；仅在你信任这个会话的操作范围时这样做 |

## 安全提醒

MCP Adapter 进程只读取 `<state-root>/tls/certificate.pem`（非机密材料，用于验证 Master 的
TLS），**从不读取 Worker API Key 或 Ed25519 私钥**。这意味着它不需要 root——只要运行它的
OS 账号是 state-root 属组的成员就够了，这也正好吻合最初的设计：Submitter 本就该是低权限的
`zz_chentian`，而不是 root。这一条对四家 Agent 一视同仁：谁来跑 `python3 -m agent_scheduler.cli.main mcp` 都是
同一个低权限进程，权限模型不因换了哪家 Agent 而改变。

在验证用的主机上，Python 解释器安装在 `/root` 下，而 `/root` 本身对非 root 账号
不可遍历——这是该主机的一个独立限制，与本节描述的权限修复无关。如果你的部署里解释器不在
`/root` 下，这条限制不适用。

不要把这个进程暴露给不可信的调用方——证书路径可读不代表进程本身可以被任意人启动。

`--username` 只是设置 `X-Username` 头，**控制面不做真实认证**——任何能连上端口的人都能冒充
`zz_chentian`。这是已知的 MVP 边界（见 `docs/agent-task-scheduler-spec.md`），不是配置错误。

## 相关文档

- [使用文档](usage.md) — 完整的安装、配置、REST/MCP 参考、运维与排障
- [测试 Submitter](testing-the-submitter.md) — Claude ​Code/Codex CLI/pi/dsh 分章节的 T1/T2/T3 验证方法
- [系统 Spec](agent-task-scheduler-spec.md) — 架构与设计约束
- `.agents/skills/submit-gpu-task/reference/proposal-template.md` — 真实通过评审的 Proposal 模板
