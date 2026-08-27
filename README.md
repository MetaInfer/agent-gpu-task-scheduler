# Agent GPU Task Scheduler MVP

面向可信内部团队的 Agent 驱动 GPU 任务调度器。系统把 Proposal 协商、独立 Reviewer、确定性签名编译、GPU/容器 Lease、Worker WSS、预建 Docker 生命周期、观察界面和 Admin 恢复串成可审计闭环。

## 资格范围

- 单个真实 Worker，8 张 `K100_AI`；生产默认 `VRAM% < 2%`，资格 profile显式使用 `<97%`。
- 唯一复用容器：`fh-sglang-deepseek-v4-flash`；严格串行，Task 后必须 stopped。
- Python `>=3.10`，当前开发环境为 3.12。
- 代码模型允许 `TaskUnit <= max_workers`；0.2.0 只资格验证一个 Worker/Unit。
- 真实任务使用版本化 launcher和 SHA-256，分别完成1、2、4、8卡 ROCm all-reduce与GEMM数值校验。
- NFS Ground Truth默认位于 `/public/share/agent-scheduler-mvp`。

**代码与 Fake 门禁通过不代表真实资格完成。** 只有 `python3 -m agent_scheduler.cli.main qualify` 验证完整证据包后才能宣称 Goal完成；外部前置不足时结果为 `BLOCKED_QUALIFICATION`。

## 文档

- [使用文档](docs/usage.md) — 安装、配置、启动、提交、运维、排障的完整指南
- [Agent Client 接入（公开）](docs/submitting-from-an-agent-client.md) — Client 操作者不接触服务端源码，只安装 Client Kit、MCP 与 skill
- [服务方内部 Agent 联调](docs/submitting-from-an-agent-session.md) — Provider 工程师启动 Master/Worker 并验证四种 harness
- [测试 Submitter（内部）](docs/testing-the-submitter.md) — Provider 工程师使用的四 harness T1/T2/T3 测试与排障指南
- [系统 Spec](docs/agent-task-scheduler-spec.md) — 架构与设计约束
- [资格状态](docs/qualification-status.md) — 真实资格证据

## 本地门禁

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest
python3 -m ruff check .
python3 -m mypy src packages/client/src
```

真实测试必须显式 opt-in，分三层，成本依次上升：

```bash
# T1：先确认 shell 没有 RUN_REAL_*；严格零成本命令见 docs/testing-the-submitter.md

# T2/T3 必须消费重新构建并完整验证的解压 Kit；缺失时在计费 Agent 启动前 skip
export AGENT_SCHEDULER_CLIENT_KIT=/absolute/path/to/agent-client-kit-0.2.0

# T2：单个 Agent 真实连通性检查——建一个 Proposal 就停，不跑 GPU
RUN_REAL_CLAUDE=1 python3 -m pytest tests/test_real_onboarding.py -m real_claude
RUN_REAL_CODEX=1  python3 -m pytest tests/test_real_onboarding.py -m real_codex
RUN_REAL_PI=1     python3 -m pytest tests/test_real_onboarding.py -m real_pi
RUN_REAL_DSH=1    python3 -m pytest tests/test_real_onboarding.py -m real_dsh

# T3：单个 Agent 完整 1/2/4/8 卡资格闭环——真实 GPU，复用容器严格串行，一次只跑一个
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CLAUDE=1 \
  python3 -m pytest tests/test_real_qualification.py -m 'real_claude and real_gpu'
```

T2 需要 Master 已用 `AGENT_SCHEDULER_HARNESS_MODE=fake` 启动（见下）；T3 需要
`AGENT_SCHEDULER_HARNESS_MODE=claude`（Processor/Reviewer 始终是 Claude，与被测试的
Submitter harness 无关）且 Worker 已连接。服务方执行这些测试所需的四个 harness 前置见
[服务方内部 Agent 联调](docs/submitting-from-an-agent-session.md)；只运行 Client Kit 的操作者请见
[Agent Client 接入](docs/submitting-from-an-agent-client.md)。

## 初始化

一次性创建 Worker Key、Ed25519 keypair和 loopback TLS证书：

```bash
python3 -m agent_scheduler.cli.main init-runtime \
  --state-root /public/share/agent-scheduler-mvp
```

若任一身份文件已存在，命令会拒绝覆盖。仓库现有 `.env` 不由项目读取；运维 shell 应在启动前显式加载所需环境。真实 Claude 角色使用受限非交互调用（`--print --setting-sources ""`，禁用内置工具/slash command/session/外部 settings），认证由父进程提供 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`。

## 启动真实闭环

三个终端使用相同环境：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=claude
export AGENT_SCHEDULER_WORKER_MODE=remote
```

终端 1：loopback HTTPS/WSS Master。

```bash
python3 -m agent_scheduler.cli.main serve
```

终端 2：root Worker，主动建立 WSS并每10秒上报真实 `hy-smi`。

```bash
python3 -m agent_scheduler.cli.main worker
```

终端 3：真实 Submitter 通过本地 MCP Adapter 一次提交四个 Proposal，并验证持久证据。
默认是 Claude ​Code；`--harness` 可选 `codex`/`pi`/`dsh`。四者都必须消费同一个重新构建并
完整验证的 Client Kit，最终启动 Kit 临时 venv 中的 `agent-scheduler-submitter`；
Processor/Reviewer 始终跑 Claude，与 `--harness` 无关：

```bash
export AGENT_SCHEDULER_CLIENT_KIT=/absolute/path/to/agent-client-kit-0.2.0
python3 -m agent_scheduler.cli.main qualify [--harness claude|codex|pi|dsh]
```

浏览器观察页为 `https://127.0.0.1:8443/`。开发证书是自签名 loopback证书；远程访问使用 SSH tunnel。

## Admin

```bash
python3 -m agent_scheduler.cli.main tick --actor admin --reason 'manual scheduler pass'
python3 -m agent_scheduler.cli.main drain --actor admin --reason maintenance
python3 -m agent_scheduler.cli.main compile-retry --proposal-id PROP --actor admin --reason fixed-validator
python3 -m agent_scheduler.cli.main reconcile --execution-id EXEC --actor admin --reason verified-stopped
python3 -m agent_scheduler.cli.main reload-users --users zz_chentian --actor admin --reason policy-update
```

所有状态变更走 loopback管理面并写审计；没有单 GPU强制释放命令。

## 已知边界

MVP 不实现真实认证、多租户隔离、镜像 pull、动态依赖安装、业务自动重试、数据库、HA、崩溃自动恢复、真实多 Worker/gang或性能 SLO。Master/Worker/root容器属于可信管理域；目标 Submitter OS身份是 `zz_chentian`，0.2.0 首版资格允许 root Claude Code。

非 root 的 Submitter 账号能读取 loopback TLS 证书（见 `docs/usage.md` §3），但如果部署主机
把 Python 解释器装在只有 root 能进的目录下（例如 `/root`），命令本身仍会失败——这是
主机配置问题，不是本项目代码的权限模型问题，需要单独把解释器装到 Submitter 账号可达的路径。
