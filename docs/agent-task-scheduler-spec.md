# Agent 驱动 GPU 任务调度框架规范

- 文档状态：已确认（MVP）
- 规范版本：0.2.0
- 最后更新：2026-08-20
- 历史基线：Git commit `b2319d8` 中的 0.1.0
- 决策来源：[Agent Task Scheduler Grilling QA](./agent-task-scheduler-grilling-qa.md)

## 1. 规范约定

本文是 0.2.0 MVP 的权威产品与技术规范。关键词 **MUST**、**MUST NOT**、**SHOULD**、**MAY** 表示约束强度。发生冲突时按以下顺序解释：

1. MUST/MUST NOT、可执行 Schema 和状态转换；
2. 失败处理与验收矩阵；
3. REST、MCP、WebSocket 和 Admin 契约；
4. 示例。

0.1.0 已由 Git 永久保留，不再在本文中混入双节点、新建容器等已延期要求。

## 2. Goal

在单台 8×K100_AI 节点上，构建并真实验证一个由 Claude Code 驱动、具备确定性签名编译、GPU 调度、Docker 生命周期管理、完整 REST/MCP/观察界面及审计恢复能力的任务调度 MVP，成功完成 1、2、4、8 卡 all-reduce 与 GEMM 任务。

权威闭环：

```text
Submitter Agent 提交 Markdown Proposal
  -> Proposal Processor 澄清并生成不可变 revision/Facts
  -> Submitter Agent 显式确认 revision
  -> 独立 Senior Reviewer 审核
  -> 确定性 Compiler 生成签名 Task
  -> Scheduler 分配 GPU、容器和 Lease
  -> Worker Driver 执行预建 Docker 容器
  -> 验证日志/output、停止容器、释放或保留资源
```

## 3. MVP 范围

### 3.1 MUST

MVP MUST：

1. 接受固定标题、正文自由的 Markdown Proposal；
2. 支持 Processor 多轮澄清、显式 revision 确认和独立 Reviewer；
3. 将冻结输入确定性编译成严格、不可变、可验签的 Task；
4. 在一个真实 Worker 上调度 1–8 张 K100_AI 整卡；
5. 严格串行复用指定的预建容器；
6. 对 Task、GPU、容器和 Lease 执行可审计状态转换；
7. 提供 REST、Submitter MCP Adapter、匿名观察 REST/网页、Worker WSS 和 Admin CLI；
8. 通过 1、2、4、8 卡真实 Claude + Docker + all-reduce/GEMM 资格验证；
9. 实现五类关键失败验收；
10. 将不可变对象、Harness记录和事件持久化到 NFS Ground Truth。

### 3.2 明确延期

0.2.0 不包含：

- 真实多 Worker/gang 部署和测试；
- 新建容器模式、多预建容器和并发复用；
- 对恶意或失控 root Worker 的硬隔离；
- 真实用户认证、Secret 管理和多租户隔离；
- Python 3.10/3.11 实测、外部 CI；
- 数据库、HA、网络分区一致性、崩溃自动恢复；
- Task 自动业务重试、checkpoint、断点续跑；
- Kubernetes、Slurm、镜像构建或自动 pull；
- Notebook、SSH、交互 Shell、常驻服务；
- 性能 SLO、all-reduce/GEMM 吞吐门槛；
- 原 0.1.0 中未纳入第 18 节的其余故障矩阵。

代码领域模型 MUST 允许 `1 <= TaskUnit 数量 <= configured max_workers`；多 Unit 时每个 Unit必须绑定不同 Worker。该扩展点不等于多 Worker 已通过验收。

## 4. 信任与身份

- 系统面向可信内部团队，不是安全沙箱。
- Master、Worker Driver、Processor、Reviewer 和 Worker Controller以 root 运行。
- 0.2.0 不防范 root Worker 修改 Master 元数据；路径分区和权限检查仅防止误操作。
- Submitter Agent目标部署身份为 `zz_chentian`，首版资格验收 MAY 使用 root Claude Code。
- 提交 REST 的 `X-Username` 只是白名单声明，不是真实认证。
- 匿名 `/api/v1/observe/*` GET 不需要 username。
- Worker使用独立 API Key，Master 使用 Ed25519 签名 Task/Plan。
- 凭证、私钥和模型认证 MUST NOT 进入 Git、Task、argv、观察接口或普通日志。
- 项目 MUST NOT 读取仓库现有 `.env`；运维父进程负责加载环境。

### 4.1 Claude 环境

真实 Claude Code调用 MUST：

- 每次创建独立非交互进程；
- 不把 session/resume 当作事实源；
- 使用版本化 Prompt、严格 JSON Schema 和显式 MCP 配置；
- 禁用未授权 Bash/Edit 等工具；
- Processor/Reviewer超时10分钟，Worker Controller超时5分钟；
- 首次调用加最多3次重试，共最多4次尝试；
- 只对 Schema输出、限流和调用超时等明确可重试错误退避重试；
- 保存脱敏 argv、工作目录、起止时间、退出码、结构化输出和工具事件；
- 默认关闭真实模型调用，必须通过显式 opt-in 开启。

真实角色使用 `claude --print --setting-sources ""`，禁用内置工具、slash command、session持久化和外部 settings/MCP；父进程仅通过最小环境 allowlist 传入 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`（0.2.0 授权使用后者）。项目不读取 `.env`，不复用 `/root` 私有安装。

## 5. 固定部署

- 单节点同时运行 Master、`worker-local-01`、Docker 和 8 张 K100_AI（ID 0–7）。
- 节点挂载 `/public/share` NFS。
- Ground Truth默认根为 `/public/share/agent-scheduler-mvp`。
- Master/Worker使用 Python `>=3.10`；当前资格环境使用 Python 3.12。
- Master仅绑定 `127.0.0.1`，REST/网页使用 HTTPS，Worker使用 WSS。
- 远程访问应通过 SSH tunnel，不直接暴露匿名观察或管理面。
- `init-runtime` 显式一次性生成 Worker Key、Ed25519 keypair和 loopback TLS证书；任何目标存在时 MUST 拒绝覆盖。
- Master只有一个实例；无数据库、无 HA。

## 6. 深模块与依赖方向

所有调用方向从外向内：Adapter -> Module interface -> domain/integrity/storage。领域状态机 MUST NOT 依赖 FastAPI、Claude Code、Docker 或 `hy-smi` 事件格式。

| Module | Interface | 隐藏的实现 |
|---|---|---|
| Proposal | `create/reply/confirm/resume/cancel/get` | 轮次、确认、审核、超时、幂等和状态转换 |
| Harness | `process/review/control` | CLI启动、权限、事件解析、重试和审计 |
| Compiler | `compile(frozen_input)` | 字段映射、默认值、canonicalization、hash、签名 |
| Event Store | `write_immutable/append/list/rebuild` | NFS布局、sequence、fsync、rename和索引 |
| Scheduler | `enqueue/tick/reconcile` | FIFO/aging、选择、hold、Lease、Plan和资源不变量 |
| Worker Driver | `execute(plan, task)` | `hy-smi`、Docker argv、前台进程、日志、超时和清理 |
| REST/MCP/UI/Admin | 对应领域用例 | HTTP/MCP/HTML/CLI序列化与错误映射 |

Fake Harness、Fake Driver、Fake clock和真实 Adapter MUST 通过相同 interface 测试。

## 7. 严格对象契约

所有持久对象和协议消息 MUST：

- 含 `schema_version: "v1"`；
- 使用 Pydantic strict model和导出的 JSON Schema；
- `additionalProperties: false`；
- 未知版本、字段、enum和越界值一律拒绝；
- 持久化时间为 UTC RFC3339；运行超时使用单调时钟；
- ID使用类型前缀加 RFC 9562 UUIDv7，如 `task_<32 hex>`。

核心不可变对象：Revision、ProposalFacts、Review、TaskUnit、Task、PrepareManifest、ExecutionPlan。可变生命周期对象的每次变化必须追加 Event。

### 7.1 TaskUnit

每个 Unit MUST：

- 申请 1–8 张 `K100_AI`；
- 绑定一个 Worker；
- 使用一个精确白名单预建容器；
- 冻结 repository digest、container user、setup/run/teardown、required logs/outputs和超时；
- run至少有一个有界前台命令；
- 禁止 `&`、`nohup`、job control、daemonize和 double-fork等提前返回方式。

### 7.2 命令类型

支持：

1. inline Bash：通过 `docker exec -i ... /bin/bash -s --` 从 stdin传入；
2. container path Bash：绝对路径 + SHA-256，执行前验 hash，再以 argv安全执行。

Docker CLI MUST 使用 argv数组，不得把未经验证的用户字符串拼入 `bash -c`。任务不得动态安装依赖。

## 8. Proposal 状态机

Proposal流程：

```text
CLARIFYING -> AWAITING_CONFIRMATION -> IN_REVIEW
IN_REVIEW -> APPROVED | CHANGES_REQUESTED | REJECTED
CHANGES_REQUESTED -> CLARIFYING
APPROVED -> COMPILING -> COMPILED | COMPILE_FAILED
```

- 初始 Markdown和单条回复各不超过256 KiB。
- 初稿固定标题全部存在，送审前不得含 `TBD`。
- 每轮生成完整不可变 revision和绑定 Facts。
- 提交者 MUST 调用 `confirm_revision(revision_id)` 才能送审。
- Reviewer只能返回 `APPROVE`、`REQUEST_CHANGES`、`REJECT`，不能修改 Proposal。
- Processor最多8个往返；Reviewer最多4次审核。
- 等待提交者单轮30分钟；Proposal总生命周期7天。
- 终局 `REJECTED/EXPIRED/CANCELLED` 无出边。
- Harness最终失败进入可恢复 `PROCESSING_ERROR`。
- Compiler失败进入 `COMPILE_FAILED`，Admin只能使用原冻结 Compilation Context重试。

## 9. 确定性编译与完整性

Compiler完整输入是：冻结 revision、Facts、Review、Policy Snapshot、Schema版本和 Compilation Context。

首次编译前 MUST 持久化 `task_id`、`execution_id`、`created_at`。相同完整输入和相同签名 key重复编译 MUST 产生逐字节相同的 unsigned canonical bytes、content hash和签名。

Task、PrepareManifest、ExecutionPlan MUST 使用：

- RFC 8785 JSON Canonicalization Scheme；
- SHA-256 content hash；
- Ed25519签名；
- 明确 `key_id`；
- integrity字段不包含在自身签名载荷中。

Worker MUST 在产生任何 Docker副作用前拒绝：未知 Schema版本、hash不匹配、签名无效、旧 `lease_epoch`、Plan与 assignment/generation/Lease不一致。

## 10. NFS Ground Truth

目录布局：

```text
/public/share/agent-scheduler-mvp/
  immutable/{proposals,revisions,facts,reviews,tasks,plans,harness}/
  events/{proposals,tasks,workers,admin}/
  snapshots/
  framework-logs/<task>/<unit>/<execution>/
  worker-inbox/<worker>/<assignment>/
  outputs/
  secrets/
  tls/
```

- Master是 Proposal、Task、Plan、调度、Harness记录和审计的唯一逻辑写者。
- `tls/` 内的证书是非机密材料，属组随 state-root；Submitter 只需读它，不需要 root 或 `secrets/` 访问权限。
- 单写入队列串行化状态变化。
- API成功响应必须晚于对应事件 fsync。
- 不可变对象使用同目录临时文件、fsync、atomic rename、目录 fsync。
- 同一对象事件为 NDJSON，sequence从1严格递增，append后不得修改。
- 读取遇到非法 JSON、sequence跳跃或中间损坏 MUST 拒绝 READY。
- 尾部半行不得被静默忽略。
- 快照可删除并从不可变对象和事件重建，不是 Ground Truth。
- Harness记录与审计永久保留；Framework日志保留30天；业务output管理员手工清理。

## 11. GPU 与队列

生产 profile准入条件：

- Worker在线；
- `hy-smi`样本不超过30秒；
- GPU无Lease和本地Assignment；
- `VRAM% < 2%`；
- 容器锁可取得。

0.2.0真实资格 profile明确覆盖生产门槛：授权 GPU 0–7，使用 `VRAM% < 97%`。该门槛由运维显式批准，用于容纳同节点其他租户容器的常驻显存；profile必须显式配置并在事件中审计，不得成为生产默认值，调度器也不得为了让资格通过而自行抬高门槛。

Worker每10秒采集一次 `hy-smi`。理论空闲但超过当前 profile门槛的 GPU为 `DRIFTED`。连续3次低于门槛后恢复 `AVAILABLE`。

队列初始 `NORMAL`；累计排队1小时变为 `AGED`；AGED严格优先，级内按首次入队时间和 task_id确定性排序。0.2.0同一预建容器严格串行，所以四个资格 Task必须依次执行。

若资格 Task超过门槛，最多等待30分钟，期间保持 queued/blocked；不得自动提高门槛。超时后资格状态为 `BLOCKED_QUALIFICATION`，不是业务 Task终态。

## 12. 调度与 Lease

单 Worker屏障顺序：

```text
SELECT -> provisional hold -> PREPARE -> PREPARED
-> COMMIT_LEASE -> SIGN_PLAN -> PLAN_ACK
-> START_SETUP -> setup complete -> START_RUN
```

任一屏障完成前不得产生下一阶段副作用。

每个 GPU和容器资源维护单调递增、不复用的 `lease_epoch`。provisional hold时分配 epoch；Attempt撤销也不能回收。旧 generation/epoch消息即使签名有效也必须拒绝。

资源不变量：

- 任一 GPU/容器同一时刻最多属于一个有效资源集合；
- 容器串行锁和全部 GPU Lease原子提交或原子撤销；
- setup开始前若能证明无副作用，Attempt MAY安全撤销并回队；
- 一旦 setup开始，不得创建新 generation或自动重试；
- 证明不完整进入 `RECONCILIATION_REQUIRED`，不得部分释放。

Task主状态：

```text
CREATED -> QUEUED/BLOCKED -> PREPARING -> RESERVED -> DISPATCHED
-> STARTING -> RUNNING -> FINALIZING
-> COMPLETED | FAILED | CANCELLED | CLEANUP_FAILED
```

`RECONCILIATION_REQUIRED`表示资源真值无法确定；终态无正常出边。

## 13. Worker WSS 协议

Worker主动连接 Master；Master沿连接 push。握手使用 `worker_id + API Key`，只接受配置中的 Worker。

每条消息 MUST 包含 `schema_version`、`message_id`、`sequence`、`assignment_id`、`dispatch_generation`、`lease_epoch`和 payload。发送方先持久化再发送；接收方登记后 ACK。重复消息只返回已登记阶段，不重复取得锁、创建 supervisor或执行 Docker。

30秒未 ACK时 Master先查询 Worker状态，不得直接重派。Worker短断重连后按 sequence补报。terminal与Harness ACK必须保留到 Master持久化确认，不能被普通事件缓存淘汰。

## 14. 指定预建容器

唯一白名单三元组：

```text
(worker-local-01, fh-sglang-deepseek-v4-flash, zz_chentian)
```

容器内执行用户为 root。冻结基线：

- repository digest：`harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:158bdfd1567477cc4d7b276ba9328b2d29b9c8bcd996d11921a9ea855dbfb238`；
- privileged、host network、host IPC、16 GiB shm；
- `/public/share -> /data` RW；
- `/public/home/zz_chentian -> /home` RW；
- `/mnt/nvme1/models -> /models` RO；
- `/opt/hyhal -> /opt/hyhal` RO；
- `/dev/kfd`、`/dev/mkfd`、`/dev/dri`可见；
- 不挂 Docker socket。

每次领取前 MUST inspect并确认：容器存在、stopped、digest和基线匹配、无其他Assignment持锁。生命周期：

```text
inspect stopped -> docker start -> setup exec -> run exec
-> best-effort teardown -> docker stop --time 30
-> if needed docker kill -> inspect stopped
```

取消、超时和强制结束 MUST 跳过 teardown并立即 stop/kill。Task终态后容器保持 stopped。

Docker命令退出码不是容器真值：

- stop返回非0但 inspect证实 stopped，可保留业务结果并记录 warning；
- stop/kill返回0但 inspect仍running，必须 `CLEANUP_FAILED`；
- 无法确认 stopped时保留 GPU/container Lease，等待 Admin原子 reconcile。

## 15. 完成语义

`COMPLETED`必须同时满足：

1. 所有 run退出码为0；
2. 所有 required业务日志存在；
3. 所有 required output存在；
4. 容器经 inspect确认 stopped；
5. Harness记录和终态事件已持久化；
6. 可释放 Lease已在同一状态提交中释放。

`COMPLETED`只表示框架执行成功，不表示自然语言 Success Criteria或训练指标达标。

## 16. REST、MCP、网页与 Admin

### 16.1 REST

权威写接口至少包括：

- `POST /api/v1/proposals`；
- `POST /api/v1/proposals/{id}/replies`；
- `POST /api/v1/proposals/{id}/confirm`；
- `POST /api/v1/proposals/{id}/resume`；
- `POST /api/v1/proposals/{id}/cancel`；
- `GET /api/v1/proposals/{id}`；
- `GET /api/v1/proposals/{id}/events`；
- `GET /api/v1/tasks/{id}`；
- 日志 byte-offset读取接口。

创建 Proposal、回复和 resume MUST 提供幂等键。键作用域为 `(username, operation, idempotency_key)`；同内容返回原结果，不同内容返回409。所有响应含 request_id。错误对象统一包含 `error_code/message/object_id/current_state/request_id/retryable`。

### 16.2 Submitter MCP Adapter

MCP Adapter在提交 Agent本地运行，只调用 REST，不直接写 NFS。至少暴露 create、reply、confirm、resume、cancel、get、wait_for_events和get_logs工具。username从启动环境固定读取，单次调用不能改变。

### 16.3 匿名观察

`/api/v1/observe/*`只允许 GET；任何非 GET返回405。观察面展示 Worker、8张GPU、Proposal、Review、队列、Task、Unit、Plan、容器、Lease、日志和审计。不得返回 Worker Key、Ed25519私钥或模型凭证。

官方网页只调用 observe GET，每5秒轮询；日志默认尾部1000行并支持 byte offset。

### 16.4 Admin

Admin CLI/loopback管理面至少支持：

- inspect阻塞资源和事件；
- drain Master；
- 用冻结 Compilation Context重试编译；
- 原子 reconcile完整 execution资源集合；
- reload白名单。

Admin不得直接绕过状态机编辑 NFS。每个变更写 actor、reason、request_id、前后状态和命令输出。不得提供单 GPU `release-gpu`。

## 17. 真实 all-reduce/GEMM 资格任务

四个独立 Proposal一次提交，分别申请1、2、4、8卡，按 FIFO和容器锁串行执行。四个任务均使用真实 Claude Submitter、Processor和Reviewer；首版 MAY使用 root Claude CLI。

脚本 MUST：

- 位于仓库并版本化；
- 通过 `/public/share` 映射到容器 `/data/...`；
- 以容器绝对路径 + SHA-256冻结到 Task；
- 使用 `torchrun --standalone --nproc_per_node N`；
- 每个 rank成功初始化；
- all-reduce结果等于解析期望值；
- 每张分配卡执行固定小矩阵 GEMM并数值校验；
- 记录耗时但无性能门槛；
- rank 0写 required JSON output和业务日志；
- 总超时10分钟（setup 2、run 5、cleanup 2分钟预算）。

若容器缺少 PyTorch、分布式 backend或所需设备能力，Task失败并报告，不得 pip install或修改基线。

## 18. 强制验收

### 18.1 本地门禁

以下必须全部通过：

- `python3 -m pytest`；
- `python3 -m ruff check .`；
- `python3 -m mypy src`；
- 严格 Schema拒绝 unknown字段/版本/enum；
- RFC 8785/Ed25519外部 golden vector；
- 相同冻结输入逐字节确定性；
- 快照删除后事件重建同一状态；
- Fake Harness/Driver完整快乐路径；
- REST、MCP、observe/UI和Admin入口无 `501`或假成功。

### 18.2 五类关键失败

1. Reviewer `REQUEST_CHANGES`后生成、再次确认新 revision；
2. 幂等键同 key异内容返回409且无第二事件/Harness调用；
3. Task/hash/signature被修改后 Worker在 Docker副作用前拒绝；
4. GPU超过当前 profile门槛时保持排队；
5. stop/kill后无法确认 stopped进入 `CLEANUP_FAILED`，保留资源，Admin只能全释放或零释放。

### 18.3 真实资格

Fake门禁通过后，系统获授权自动启动指定容器并连续运行1/2/4/8卡任务，无需再次确认。每个任务必须保存：

- Claude CLI版本、角色、脱敏调用记录和结构化结果；
- Proposal/revision/Facts/Review；
- canonical Task bytes、hash、签名和验签证据；
- `hy-smi`原始样本、选择的GPU ID和Lease；
- Docker inspect/start/exec/stop/kill证据；
- all-reduce/GEMM JSON output、业务日志和Framework日志；
- 容器最终 stopped与资源释放事件。

## 19. Goal 完成与阻塞

只有下列条件全部满足，Goal才完成：

- spec、Schema、实现、测试和运维文档一致；
- 所有本地门禁和五类失败通过；
- REST/MCP/UI/Admin在0.2.0范围内真实可用；
- 四个任务均由真实 Claude角色驱动；
- 1/2/4/8卡真实 all-reduce与GEMM全部数值正确；
- 每个任务后容器和资源得到权威清理确认；
- Git阶段提交完整。

若 Claude认证不可用、GPU 30分钟内不满足门槛、容器依赖缺失或真实资格失败，则实现阶段可以完成，但总 Goal必须标记 **`BLOCKED_QUALIFICATION`**，不得宣称完成。
