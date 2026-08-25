# 从一个 Claude 会话提交 Proposal

回答一个具体问题：手上有一个 Claude Code 会话，怎么让它给调度器发 Proposal？

**简短答案**：MCP 是**必须**的，skill 是**强烈建议**的，两者作用不同。

| | 作用 | 必要性 | 不装的后果 |
| --- | --- | --- | --- |
| MCP Server | **通路** — 让会话能调用控制面 | 必须 | 会话根本没有可用的工具，只能让你手动跑 curl |
| Skill | **知识** — 告诉它 Proposal 怎么写、评审被拒怎么办 | 强烈建议 | 工具能调，但内容几乎必然被 `422` 拒绝 |

第二行不是理论。首次真实资格运行就是这么失败的：Claude 自己发明了
`--nproc-per-node`、`--output`、`--log` 四个 flag，而冻结 launcher 只收两个位置参数，
连续 7 次被控制面拒绝。它不知道契约，就一定会猜。

本仓库已经把两样都准备好了：

- MCP 配置模板 `config/submitter-mcp.example.json`
- Skill `.claude/skills/submit-gpu-task/`（含真实通过评审的 Proposal 模板）

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

uv run agent-scheduler serve     # 终端 1
uv run agent-scheduler worker    # 终端 2
```

确认：

```bash
curl -sk https://127.0.0.1:8443/health
```

`workers` 必须是 `1`，`integrity` 必须是 `valid`。**这一步不通过，后面全都白搭。**

> Master 起不来最常见的原因是端口被上一次没退干净的进程占着。`serve` 会打印
> `address already in use` 然后静默退出，而旧进程仍在服务旧代码——症状是「我明明改了却没生效」。
> 用 `ss -lntp | grep 8443` 确认。

`AGENT_SCHEDULER_HARNESS_MODE=claude` 意味着 Processor 和 Reviewer 会真的调用 Claude，
**会产生费用**。只想跑通链路不想花钱，把它设成 `fake`——除了 Processor/Reviewer 是假的，
其余（签名编译、调度、租约、Docker、产物校验）全是真的。

## 步骤 2 · 给会话装 MCP Server

三选一。

### 方式 A：`claude mcp add`（推荐，作用域可控）

```bash
cd /public/share/fh/agent-gpu-task-scheduler

claude mcp add submitter \
  --scope project \
  --env AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp \
  -- uv run agent-scheduler mcp \
       --base-url https://127.0.0.1:8443 \
       --username zz_chentian
```

`--scope project` 写进项目配置；换成 `user` 则对你所有会话生效。

### 方式 B：手写 `.mcp.json`

把 `config/submitter-mcp.example.json` 的内容复制到仓库根的 `.mcp.json`：

```bash
cp config/submitter-mcp.example.json .mcp.json
```

在这个目录里启动的 Claude 会话都会自动加载它。**注意这是全局副作用**——所以仓库里默认
只放 `.example`，不放生效的 `.mcp.json`，要不要开由你决定。

### 方式 C：一次性会话

```bash
claude --mcp-config config/submitter-mcp.example.json
```

不落任何配置，适合试一次。

### 确认装上了

在会话里执行 `/mcp`，应该看到 `submitter` 处于 `connected`，并列出 12 个工具：

```
create_proposal   reply           confirm_revision   get_reviews
get_proposal      resume          cancel             get_task
cancel_task       wait_for_task   wait_for_events    get_logs
```

命令行确认用 `claude mcp list`，会显示 `✔ Connected`。

> **`Connected` 不代表整条链通。** Adapter 的 `initialize` 和 `tools/list` 是本地应答的，
> 不碰 REST——Master 没起也照样显示 `Connected`。真正的连通性以步骤 1 的 `/health` 为准。

## 步骤 3 · 装 Skill

Skill 已经在仓库里了。在这个目录启动的会话会自动发现
`.claude/skills/submit-gpu-task/`，无需额外操作。

想让它对所有项目可用：

```bash
cp -r .claude/skills/submit-gpu-task ~/.claude/skills/
```

用 `/skills` 确认它出现在列表里。

## 步骤 4 · 触发

**不需要背特定关键词。** Skill 的 `description` 字段就是触发条件，Claude 会按语义匹配。
自然说人话即可：

- 「跑一个 4 卡的 GPU 任务」
- 「提一个 8 卡的 all-reduce Proposal」
- 「submit a GPU task using 2 cards」

想强制触发就打斜杠命令：

```
/submit-gpu-task 用 8 卡跑一次 all-reduce 和 GEMM 校验
```

只要它调用了 `mcp__submitter__create_proposal`，链路就通了。

---

## 会话内部会发生什么

装好之后，Claude 会走这条路径（skill 里写死了）：

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
补上 bind mount 声明后全部通过。这说明评审门是真在工作。

## 手动验证（不经过 Claude）

想确认通路本身没问题，可以直接喂 JSON-RPC 给 Adapter：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| uv run agent-scheduler mcp --base-url https://127.0.0.1:8443 --username zz_chentian
```

能列出 12 个工具就说明 Adapter、TLS、控制面这条链是通的，问题只可能在 Claude 侧配置。

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/mcp` 里没有 `submitter` | 配置没被加载 | 确认启动目录，或改用 `--scope user` |
| `submitter` 显示 `failed` | Adapter 进程起不来 | 用上面的手动验证命令看真实报错 |
| `mcp requires --username` | 没传 `--username` | 加参数，或设 `AGENT_SCHEDULER_USERNAME` |
| 连接被拒 / TLS 失败 | Master 没起，或 `AGENT_SCHEDULER_STATE_ROOT` 不对 | Adapter 用 `<state-root>/tls/certificate.pem` 作 CA，路径必须和 Master 一致 |
| `403 USERNAME_NOT_ALLOWED` | 用户名不在白名单 | 用 `zz_chentian`，或 `reload-users` 加人 |
| `422 INVALID_PROPOSAL` | 内容不合契约 | 读 `message`。最常见是 argv 不是两个位置参数 |
| `409 IDEMPOTENCY_CONFLICT` | 同键不同负载 | 换新幂等键 |
| Task 长期 `BLOCKED` | 准入不满足 | 看观察界面 GPU 状态；容器严格串行，排队正常 |
| 权限不足读不到证书 | Submitter 账号不在 state-root 的属组里 | `stat -c '%G' <state-root>` 查看属组，把 Submitter 账号加进该组；证书路径是 `<state-root>/tls/certificate.pem`，不是 `secrets/` 下 |

## 安全提醒

MCP Adapter 进程只读取 `<state-root>/tls/certificate.pem`（非机密材料，用于验证 Master 的
TLS），**从不读取 Worker API Key 或 Ed25519 私钥**。这意味着它不需要 root——只要运行它的
OS 账号是 state-root 属组的成员就够了，这也正好吻合最初的设计：Submitter 本就该是低权限的
`zz_chentian`，而不是 root。

在验证用的主机上，`uv` 托管的 Python 解释器安装在 `/root` 下，而 `/root` 本身对非 root 账号
不可遍历——这是该主机的一个独立限制，与本节描述的权限修复无关。如果你的部署里解释器不在
`/root` 下，这条限制不适用。

不要把这个进程暴露给不可信的调用方——证书路径可读不代表进程本身可以被任意人启动。

`--username` 只是设置 `X-Username` 头，**控制面不做真实认证**——任何能连上端口的人都能冒充
`zz_chentian`。这是已知的 MVP 边界（见 `docs/agent-task-scheduler-spec.md`），不是配置错误。

## 相关文档

- [使用文档](usage.md) — 完整的安装、配置、REST/MCP 参考、运维与排障
- [系统 Spec](agent-task-scheduler-spec.md) — 架构与设计约束
- `.claude/skills/submit-gpu-task/reference/proposal-template.md` — 真实通过评审的 Proposal 模板
