# Agent GPU Task Scheduler 使用文档

适用版本 0.2.0。本文覆盖安装、初始化、配置、启动、提交任务、观察、运维、排障与边界。
架构与设计约束见 [`agent-task-scheduler-spec.md`](agent-task-scheduler-spec.md)，真实资格证据见
[`qualification-status.md`](qualification-status.md)。

---

## 1. 系统概览

四类进程，全部在单节点上：

| 进程 | 命令 | 身份 | 职责 |
| --- | --- | --- | --- |
| Master | `agent-scheduler serve` | root | REST 控制面、WSS 服务端、调度器、观察界面 |
| Worker | `agent-scheduler worker` | root | 主动外连 WSS、采集 `hy-smi`、管理容器生命周期 |
| MCP Adapter | `agent-scheduler mcp` | Submitter | stdio JSON-RPC，把 Agent 工具调用翻译成 REST |
| Submitter Agent | 由 `qualify` 拉起，或人工 | `zz_chentian` | 写 Proposal、应对评审、轮询 Task |

Master 内部还会以子进程方式调用 Claude 扮演两个角色：

- **Processor** — 把一版 Proposal Markdown 规范化成严格 schema 的 `ProposalFacts`
- **Reviewer** — 独立审阅，返回 `APPROVE` / `REQUEST_CHANGES` / `REJECT`

数据流：

```
Submitter Agent
   │ (MCP stdio)
   ▼
MCP Adapter ──REST──► Master ──► Processor(Claude) ──► Facts
                        │                                │
                        │        ◄── Reviewer(Claude) ◄──┘
                        │
                        ├─ 确定性签名编译 (RFC 8785 + SHA-256 + Ed25519)
                        ├─ 调度：准入 / 排队 / GPU 与容器 Lease
                        │
                        └──WSS──► Worker ──► docker start/exec/stop
                                     │
                                     └──► NFS Ground Truth（产物 / 日志 / 事件）
```

所有状态变更都落在 NFS Ground Truth，快照可删除并从不可变历史重建。

---

## 2. 安装

要求 Python `>=3.10`（开发环境 3.12）、`uv`、可用的 `docker` 与 `hy-smi`。

```bash
uv sync --extra test
```

本地门禁：

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

涉及真实计费或真实硬件的测试必须显式 opt-in：

```bash
RUN_REAL_CLAUDE=1 uv run pytest -m real_claude   # 一次计费的 Claude 调用
RUN_REAL_GPU=1   uv run pytest -m real_gpu       # 真实 hy-smi 与容器前置检查
```

---

## 3. 一次性初始化

```bash
uv run agent-scheduler init-runtime --state-root /public/share/agent-scheduler-mvp
```

在 `<state-root>/secrets/`（目录 `0700`，文件 `0600`，仅 root 可读）生成五项：

| 文件 | 用途 |
| --- | --- |
| `worker-api-key` | Worker WSS 的 Bearer 凭据 |
| `ed25519-private.pem` / `ed25519-public.pem` | 编译产物签名密钥对 |
| `ed25519-key-id` | 密钥标识，写入签名负载 |
| `tls-private-key.pem` | loopback TLS 私钥 |

另在 `<state-root>/tls/`（目录 `0750`，文件 `0640`，属组与 state-root 相同）生成：

| 文件 | 用途 |
| --- | --- |
| `certificate.pem` | loopback 自签证书——公开材料，用于客户端验证 Master，不用于认证调用方 |

证书刻意放在 `secrets/` 之外：它是非机密材料，只要 OS 账号属于 state-root 的属组就能读到，
不需要 root。这正是 `agent-scheduler mcp` 命令能以非 root 的 Submitter 账号运行的原因——
它只需要这一个文件，从不读取另外五项。

**任一文件已存在时命令直接拒绝，不覆盖。** 需要轮换必须人工归档后重建。

这些密钥永远不得进入 Git、Task 负载、argv、观察 API 或普通日志。仓库里的 `.env`
不由本项目读取，运维 shell 需在启动前自行导出环境变量。

---

## 4. 配置

全部通过环境变量，进程启动时校验，非法值直接拒绝启动。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SCHEDULER_STATE_ROOT` | `/public/share/agent-scheduler-mvp` | NFS Ground Truth 根目录 |
| `AGENT_SCHEDULER_PROFILE` | 未设置（生产） | 设为 `qualification` 启用资格 profile |
| `AGENT_SCHEDULER_VRAM_THRESHOLD` | 见下 | GPU 准入的 VRAM 上限（百分比） |
| `AGENT_SCHEDULER_HARNESS_MODE` | `fake` | `fake` 或 `claude` |
| `AGENT_SCHEDULER_WORKER_MODE` | `remote` | `fake`、`local` 或 `remote` |
| `AGENT_SCHEDULER_ALLOWED_USERS` | `zz_chentian` | 逗号分隔的 Submitter 白名单 |
| `AGENT_SCHEDULER_MAX_WORKERS` | `1` | 代码模型允许 `TaskUnit <= max_workers` |
| `AGENT_SCHEDULER_AUTO_SCHEDULE` | `1` | `0` 表示只接受 Admin `tick` 手动推进 |
| `AGENT_SCHEDULER_USERNAME` | — | MCP Adapter 的 Submitter 身份 |

### VRAM 阈值

| profile | 默认值 | 硬上限 |
| --- | --- | --- |
| 生产（未设 `PROFILE`） | `2.0` | `2.0` |
| `qualification` | `97.0` | `97.0` |

超过所属 profile 的硬上限会在启动时抛 `ValueError`。资格 profile 的放宽由运维显式批准，
必须在事件中留痕，**调度器不得为了让任务通过而自行抬高阈值**。

### Claude 角色调用契约

真实角色使用受限非交互调用：

```
claude --print --no-session-persistence --disable-slash-commands \
       --setting-sources "" --permission-mode dontAsk --tools "" \
       --allowedTools <仅 MCP 工具> --strict-mcp-config --mcp-config <生成的配置> \
       --system-prompt-file prompts/<role>.md \
       --output-format stream-json --verbose --json-schema
```

内置工具、slash command、session 持久化、外部 settings/MCP 全部禁用。
子进程只继承最小 allowlist：`HOME`、`PATH`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、
`ANTHROPIC_BASE_URL`、代理变量。两个凭据有其一即可。

失败重试为首次调用 + 最多 3 次重试（共 4 次），且仅对可重试的 stderr 生效。

---

## 5. 启动

三个终端共用同一套环境：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=claude
export AGENT_SCHEDULER_WORKER_MODE=remote
export ANTHROPIC_AUTH_TOKEN=...        # 或 ANTHROPIC_API_KEY
```

终端 1 — Master：

```bash
uv run agent-scheduler serve [--host 127.0.0.1] [--port 8443]
```

终端 2 — Worker（主动外连，每 10 秒上报真实 `hy-smi`）：

```bash
uv run agent-scheduler worker [--uri wss://127.0.0.1:8443/api/v1/worker/ws]
```

### 确认启动成功

```bash
curl -sk https://127.0.0.1:8443/health
```

```json
{"status":"ready","workers":1,"integrity":"valid",
 "qualification":true,"harness_mode":"claude","worker_mode":"remote"}
```

`workers` 必须为 1，`integrity` 必须为 `valid`。`integrity` 非 `valid` 表示事件流校验失败
（断行、序列缺口或跨流绑定错误），此时不要提交任务，先用 `inspect` 排查。

浏览器打开 `https://127.0.0.1:8443/`（自签证书；远程访问请用 SSH tunnel，不要暴露端口）。

---

## 6. 提交任务

### 6.1 通过 MCP（推荐，Agent 用）

生成一份 MCP 配置指向本地 adapter：

```json
{
  "mcpServers": {
    "submitter": {
      "command": "uv",
      "args": ["run", "agent-scheduler", "mcp",
               "--base-url", "https://127.0.0.1:8443",
               "--username", "zz_chentian"]
    }
  }
}
```

工具清单：

| 工具 | 必填参数 | 说明 |
| --- | --- | --- |
| `create_proposal` | `markdown`, `idempotency_key` | 创建 Proposal |
| `reply` | `proposal_id`, `markdown`, `idempotency_key` | 提交**完整替换**修订 |
| `confirm_revision` | `proposal_id`, `revision_id`, `idempotency_key` | 显式确认当前修订送审 |
| `get_reviews` | `proposal_id` | 读评审决定、理由与当前 Facts |
| `get_proposal` | `proposal_id` | 读 Proposal 状态 |
| `resume` | `proposal_id`, `idempotency_key` | 从可恢复状态继续 |
| `cancel` | `proposal_id` | 取消非终态 Proposal |
| `get_task` | `task_id` | 读不可变 Task 与当前状态 |
| `cancel_task` | `task_id` | 取消排队或运行中的 Task |
| `wait_for_task` | `task_id` | 轮询 Task，最长 30 秒 |
| `wait_for_events` | `proposal_id`, `after_sequence` | 轮询事件，最长 30 秒 |
| `get_logs` | `task_id`, `unit_id`, `execution_id`, `name` | 从 offset 读 Framework 日志 |

每次 create/reply/confirm 都要用**唯一**幂等键。重复键配同一负载返回缓存结果；
配不同负载返回 `409 IDEMPOTENCY_CONFLICT`。

### 6.2 通过 REST（人工调试用）

```bash
BASE=https://127.0.0.1:8443
H='-H Content-Type:application/json -H X-Username:zz_chentian'

# 创建
curl -sk $BASE/api/v1/proposals $H -H 'Idempotency-Key: create-1' \
     -d "$(jq -Rn --rawfile m proposal.md '{markdown:$m}')"

# 确认送审
curl -sk $BASE/api/v1/proposals/$PROP/confirm $H -H 'Idempotency-Key: confirm-1' \
     -d '{"revision_id":"'"$REV"'"}'

# 被要求修改后：读评审 → 提完整替换修订 → 重新确认
curl -sk $BASE/api/v1/proposals/$PROP/reviews
curl -sk $BASE/api/v1/proposals/$PROP/replies $H -H 'Idempotency-Key: reply-1' \
     -d "$(jq -Rn --rawfile m proposal-v2.md '{markdown:$m}')"

# 查 Task
curl -sk $BASE/api/v1/tasks/$TASK
```

### 6.3 一键资格闭环

```bash
uv run agent-scheduler qualify [--base-url https://127.0.0.1:8443] [--timeout N]
```

拉起真实 Claude Submitter，一次提交 1/2/4/8 卡四个 Proposal，然后独立验证完整证据包。
详见第 11 节。

---

## 7. Proposal 契约

### 7.1 Markdown 结构

必须按**这个顺序**给出全部 15 个小节，内容完整，不允许 `TBD`：

```markdown
# Proposal
## Identity
## Objective
## Success Criteria
## Workload and Code
## Container
## Resources
## Commands
## Inputs and Mounts
## Environment
## Networking and Privileges
## Timeout and Cleanup
## Framework Logs
## Business Logs and Outputs
## Multi-node Coordination
## Risks and Notes
```

每份 Proposal 必须写明：Submitter `zz_chentian`；Worker `worker-local-01`；
容器 `fh-sglang-deepseek-v4-flash`，容器用户 `root`；镜像 digest；冻结 launcher 及其 SHA-256；
Proposal 唯一的产物与业务日志路径；有界的前台命令与总超时。

### 7.2 冻结 launcher 契约

资格任务的运行命令是完全确定的，Processor 不得改写：

- `kind`：`container_path_bash`
- `container_path`：`/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh`
- `sha256`：`c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c`
- `argv`：**恰好两个位置参数**，依次为产物路径、业务日志路径。**不接受任何 flag。**
  world size 由调度器注入的 `HIP_VISIBLE_DEVICES` 推导，不走 argv。

路径约定（`<pid>` 为 `proposal_id`）：

| 字段 | 值 |
| --- | --- |
| `argv[0]` | `/data/agent-scheduler-mvp/outputs/<pid>.json` |
| `argv[1]` | `/data/agent-scheduler-mvp/logs/<pid>.log` |
| `required_outputs` | 上者把前缀 `/data` 换成 `/public/share`，恰好一项 |
| `required_logs` | 同上，恰好一项 |

容器内 `/data` 即宿主 `/public/share` 的 bind mount。产物路径在执行前必须不存在。

违反任一条会得到 `422 INVALID_PROPOSAL`，错误体里带具体原因。

---

## 8. 状态机

### Proposal

```
CLARIFYING ──► AWAITING_CONFIRMATION ──► IN_REVIEW ──┬─► APPROVED ──► COMPILING ──► COMPILED
                       ▲                             ├─► CHANGES_REQUESTED ──┐
                       └─────────────────────────────┘                       │
                       └───────────── reply ◄─────────────────────────────────┘
                                                     └─► REJECTED
```

终态：`COMPILED`、`REJECTED`、`CANCELLED`、`EXPIRED`、`COMPILE_FAILED`、`PROCESSING_ERROR`。
`AWAITING_CONFIRMATION` 有 30 分钟 Submitter 截止时间；Proposal 整体 7 天后过期。

### Task

```
CREATED ─► QUEUED ─► PREPARING ─► RESERVED ─► DISPATCHED ─► STARTING ─► RUNNING ─► FINALIZING ─► COMPLETED
             │                                                                          └─► FAILED
             └─► BLOCKED                                                                └─► CLEANUP_FAILED
                                                                                        └─► RECONCILIATION_REQUIRED
```

- `BLOCKED`：准入不满足（VRAM 超阈值、容器被占、GPU 有租约）。资格 profile 下等待超过
  30 分钟会以 `QUALIFICATION_GPU_WAIT_EXPIRED` 终止。
- `CLEANUP_FAILED` / `RECONCILIATION_REQUIRED`：容器停止未被 `docker inspect` 确认，
  **租约刻意保留**，需要 Admin `reconcile` 人工确认后释放。
- Master 重启后，原本活跃的 Task 一律标记 `RECONCILIATION_REQUIRED`，Worker 以
  offline/UNKNOWN 载入，等待重新连接。

### GPU

`AVAILABLE` / `DRIFTED` / `UNKNOWN`。理论空闲但 VRAM 超过当前 profile 阈值的 GPU 记为
`DRIFTED`；需要**连续 3 次**低于阈值才恢复 `AVAILABLE`。

---

## 9. REST API 参考

Submitter 接口需要 `X-Username` 头；写操作需要 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 就绪、Worker 数、事件完整性、当前 profile |
| POST | `/api/v1/proposals` | 创建，`{"markdown": "..."}`，201 |
| GET | `/api/v1/proposals/{id}` | 读状态 |
| POST | `/api/v1/proposals/{id}/replies` | 完整替换修订 |
| POST | `/api/v1/proposals/{id}/confirm` | `{"revision_id": "..."}` |
| GET | `/api/v1/proposals/{id}/reviews` | 评审 + 当前 Facts |
| GET | `/api/v1/proposals/{id}/events` | `?after_sequence=N` |
| POST | `/api/v1/proposals/{id}/resume` | 恢复 |
| POST | `/api/v1/proposals/{id}/cancel` | 取消 |
| GET | `/api/v1/tasks/{id}` | Task + 当前状态 |
| POST | `/api/v1/tasks/{id}/cancel` | 取消 |
| GET | `/api/v1/logs/{task}/{unit}/{exec}/{name}` | Framework 日志字节，`?offset=N` |
| GET | `/api/v1/observe/summary` | Master profile、Worker 与 GPU、Proposal、容器状态 |
| GET | `/api/v1/observe/events/{type}/{id}` | 只读事件 |
| GET | `/` | 观察界面（每 2 秒刷新，只读） |

`/api/v1/observe/**` 是 GET-only，其他方法一律 `405`。

观察界面用的就是 `observe/summary` 这一个端点，2 秒轮询。为此 summary 里的几项做了缓存与瘦身：
`docker inspect` 与目录索引缓存 2 秒，`integrity` 缓存 30 秒（它要解析全部事件文件，不能按轮询频率跑），
`framework_logs` 与 `audit_streams` 返回 `{"count", "recent"}` 而不是全量路径列表。
**不要用 `/health` 做高频轮询**，它每次都会完整校验事件流。

### 错误语义

错误体统一为 `{"error_code", "message", "object_id", "current_state", "request_id"}`。

| 状态码 | 含义 | 正确反应 |
| --- | --- | --- |
| 403 | `USERNAME_NOT_ALLOWED` | 换合法 Submitter 身份 |
| 404 | `NOT_FOUND` | 对象不存在 |
| 409 | 状态冲突（`CHANGES_REQUESTED`、`INVALID_STATE`、`IDEMPOTENCY_CONFLICT`、`ROUND_LIMIT` 等） | 读状态后走对应流程，不要盲目重试 |
| 422 | `INVALID_PROPOSAL`、`IDEMPOTENCY_REQUIRED` | **改内容**，重试没用 |
| 503 | 正在 drain | 等待或联系运维 |

MCP Adapter 会把 `error_code` 与 `message` 原样透传给 Agent，不会只丢一个状态行。

---

## 10. 运维（Admin）

全部走 loopback 管理面并写审计，必须给 `--actor` 与 `--reason`：

```bash
uv run agent-scheduler tick    --actor admin --reason 'manual scheduler pass'
uv run agent-scheduler drain   --actor admin --reason maintenance
uv run agent-scheduler compile-retry --proposal-id PROP  --actor admin --reason fixed-validator
uv run agent-scheduler reconcile     --execution-id EXEC --actor admin --reason verified-stopped
uv run agent-scheduler reload-users  --users zz_chentian --actor admin --reason policy-update
```

- `drain` 之后新提交返回 `503`，在途 Task 继续跑完。
- `reconcile` 是唯一能释放 `CLEANUP_FAILED` / `RECONCILIATION_REQUIRED` 所持租约的路径，
  且要求人工已确认容器确实停止。
- **没有单 GPU 强制释放命令**，这是刻意的。

### 检视 Ground Truth

```bash
uv run agent-scheduler inspect --state-root /public/share/agent-scheduler-mvp \
  --kind {events|immutable|snapshot|leases} \
  [--object-type TYPE] [--object-id ID]
```

### 目录布局

```
<state-root>/
├── secrets/            # 0700，仅 root 可读的密钥（不含 TLS 证书）
├── tls/                # 0750，certificate.pem 0640，属组与 state-root 相同——非 root Submitter 可读
├── immutable/          # 不可变对象，按类型分目录
│   ├── revisions/  facts/  reviews/  compilation-contexts/
│   ├── tasks/  plans/  manifests/  task-status-history/
│   ├── lease-history/  idempotency/  proposal-states/
│   ├── harness/            # 每次 Claude 调用的完整审计（argv/退出码/stdout）
│   ├── worker-samples/     # 原始 hy-smi 行
│   ├── worker-evidence/  worker-replies/
│   └── qualification-runs/  qualification-gates/
├── events/             # 追加写 NDJSON，每条 fsync
│   ├── proposals/  tasks/  workers/
├── snapshots/          # 可删除，可从 immutable 重建
├── framework-logs/     # <task>/<unit>/<exec>/run-001.json，默认保留 30 天
├── worker-inbox/       # Worker 侧证据
├── outputs/            # 业务产物
└── logs/               # 业务日志
```

`immutable/` 与 `events/` 是权威来源；`snapshots/` 删掉可重建，不要手工编辑任何一个。

---

## 11. 资格验证

```bash
uv run agent-scheduler qualify
```

流程：跑本地门禁并留证 → 检查 `/health` profile → 写 MCP 配置 → 拉起真实 Claude Submitter
一次提交四个 Proposal → 轮询到终态 → 独立验证证据包。

验证器**默认失败**，逐项检查：run_id 绑定、门禁记录、Task/Plan/Manifest 签名、
`hy-smi` 样本新鲜度（≤30 秒且早于计划创建时间）、WSS 协议事件、Worker 证据、
Docker exec 记录、产物新鲜度、逐 rank 数值输出、残留租约为零、容器已停止。

历史证据、伪造证据、过期样本都无法让它通过。

输出：

```json
{"schema_version":"v1","run_id":"qual_...","status":"COMPLETED",
 "items":[{"card_count":1,"proposal_id":"...","task_id":"...","state":"COMPLETED"}, ...],
 "reason":"..."}
```

`status` 只有 `COMPLETED` 与 `BLOCKED_QUALIFICATION` 两种。
**代码门禁与 Fake 通过不代表真实资格完成**，只有本命令返回 `COMPLETED` 才算。

---

## 12. 排障

| 现象 | 原因与处理 |
| --- | --- |
| `/health` 里 `workers: 0` | Worker 未连上。检查 Worker 日志、`secrets/worker-api-key` 是否与 Master 同一 state root |
| `/health` 里 `integrity` 非 `valid` | 事件流断行 / 序列缺口 / 跨流绑定错误。用 `inspect --kind events` 定位，不要继续提交 |
| 创建返回 `422 INVALID_PROPOSAL` | 读 `message`。最常见是 argv 不是两个位置参数，或产物路径没带 `proposal_id` |
| 创建返回 `409 IDEMPOTENCY_CONFLICT` | 同一幂等键配了不同负载，换新键 |
| 确认返回 `409 CHANGES_REQUESTED` | Reviewer 要求修改。`get_reviews` → 完整替换修订 `reply` → 用新键 `confirm` |
| Task 长期 `BLOCKED` | 看 `observe/summary` 的 GPU 状态。VRAM 超阈值、GPU 有租约或容器被占 |
| GPU 卡在 `DRIFTED` | 需要连续 3 次采样低于阈值才恢复，约 30 秒 |
| Task 进 `CLEANUP_FAILED` | 容器停止未被确认，租约刻意保留。人工确认后 `reconcile` |
| 容器内 `import torch` 报 `librocm_smi64.so.2` | 已由 ExecutionPlan 把镜像自带的 `/opt/dtk/.hyhal/rocm_smi/lib` 前置到 `LD_LIBRARY_PATH` 解决，不要改镜像 |
| Claude 角色启动失败 | 确认 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` 已导出，且 `claude` 在 `PATH` 上 |
| `qualify` 返回 `BLOCKED_QUALIFICATION` | 读 `reason`，它会指明是哪一项证据不成立 |

排查 Claude 角色行为时，`immutable/harness/` 里有每次调用的完整 argv、退出码与 stdout。

---

## 13. 已知边界

MVP **不**实现：真实认证与多租户隔离、镜像 pull、动态依赖安装、业务级自动重试、
数据库、HA、崩溃自动恢复、真实多 Worker / gang 调度、性能 SLO。

信任模型：Master、Worker、容器内 root 同属可信管理域，**不防御作恶或出错的 root Worker**。
唯一的外部身份是 Submitter（目标 `zz_chentian`；0.2.0 首版资格允许 root Claude Code）。

资源约束：单 Worker `worker-local-01`，8 张 `K100_AI`；唯一复用容器
`fh-sglang-deepseek-v4-flash`，严格串行，每个 Task 结束后必须 stopped。
调度器**永不**安装或修改容器依赖。
