# Agent GPU Task Scheduler MVP

这是一个面向可信内部团队的 Agent 驱动 GPU 任务调度 MVP。它把 Proposal 协商、Reviewer 审核、确定性编译、签名 Task、GPU 资源 Lease、预建 Docker 容器生命周期和审计事件串成一个可追踪闭环。

## 当前资格范围

- 单个真实 Worker，8 张 `K100_AI`，开发 profile 允许 `VRAM% < 90%`；生产默认仍为 `<2%`。
- 仅复用白名单容器 `fh-sglang-deepseek-v4-flash`，同一容器严格串行，任务后保持 stopped。
- 代码模型允许 `TaskUnit <= max_workers`；真实验收当前只覆盖一个 Worker 和一个 Unit。
- 真实任务脚本位于 `scripts/torch_collective_smoke.py`，覆盖 1/2/4/8 卡 all-reduce 与 GEMM 正确性。
- 默认 Ground Truth 根为 `/public/share/agent-scheduler-mvp`。

## 开发

```bash
uv sync --extra test
uv run pytest
uv run ruff check .
uv run mypy src
```

真实环境测试必须显式选择 marker：

```bash
uv run pytest -m real_claude
uv run pytest -m real_gpu
```

## 启动

```bash
uv run agent-scheduler init-runtime --state-root /public/share/agent-scheduler-mvp
uv run uvicorn agent_scheduler.api.app:app --host 127.0.0.1 --port 8443
```

`init-runtime` 不会覆盖既有密钥。真实 Claude 调用默认关闭；启用时由运维父进程显式提供 `ANTHROPIC_API_KEY`，适配器使用独立非交互进程和严格 JSON 输出。

## 重要边界

MVP 不实现真实认证、多租户隔离、镜像 pull、动态依赖安装、业务自动重试、数据库、HA、崩溃自动恢复或性能 SLO。`root` Master/Worker 属于可信管理域；Proposal Agent 的目标部署身份是 `zz_chentian`，但首版验收允许使用 root Claude Code。
