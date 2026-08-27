# 测试 Submitter：四个 Agent 的三层验证指南

回答一个具体问题：改了 Submitter 接入之后，怎么确认 Claude ​Code / Codex CLI / pi / dsh
四家都真的能用？本文档只谈**测试方法**——如何配置、如何跑、跑不通看哪里。接入配置本身
（一次性安装、MCP 声明、Skill 发现）见 [从 Agent 会话提交](submitting-from-an-agent-session.md)。

## 三层测试总览

| 层 | 内容 | 触发方式 | 成本 | 需要什么在跑 |
| --- | --- | --- | --- | --- |
| **T1** | 配置生成、契约校验、驱动侧重建逻辑 | 默认门禁的一部分 | 零成本，不发真实请求 | 无 |
| **T2** | 单个 Agent 真实建一个 Proposal 就停 | 逐个 harness 显式 opt-in | 几个 token，不碰 GPU | Master（`HARNESS_MODE=fake` 即可） |
| **T3** | 单个 Agent 完整 1/2/4/8 卡资格闭环 | 显式 opt-in，一次只跑一个 harness | 真实 GPU 时间、真实计费 | Master（`HARNESS_MODE=claude`）+ Worker |

三层递进：T1 不通，T2 必然也不通；T2 不通，先别启动 T3——T3 是整个仓库里最贵的一段，
复用容器严格串行。Codex CLI、pi、dsh 三个新增 harness 各跑 4 个真实 Task，共 12 个；
如果连已有 Claude ​Code 资格也重新跑一遍，四个 harness 合计 16 个真实 Task。

```bash
# 显式清除历史 shell 里可能残留的 opt-in，再用 marker 双重排除所有真实测试。
env -u RUN_REAL_CLAUDE -u RUN_REAL_CODEX -u RUN_REAL_PI -u RUN_REAL_DSH \
    -u RUN_REAL_GPU -u RUN_FULL_QUALIFICATION \
  uv run pytest \
  -m 'not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu'
uv run ruff check . && uv run mypy src
```

不要把裸 `uv run pytest` 当成永远零成本：真实测试靠环境变量 opt-in；如果当前 shell 残留
`RUN_REAL_*`，默认收集也可能启动计费调用。上面的命令同时清环境变量和排 marker，才是
可靠的 T1。

## 通用前置

### T2 前置：Master 用 fake harness 起

T2 只验证「这个 Agent 能不能通过它自己的接入配置把 Proposal 建出来」，不需要真实的
Processor/Reviewer，所以 Master 用 `HARNESS_MODE=fake` 最省钱：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=fake
export AGENT_SCHEDULER_WORKER_MODE=remote
uv run agent-scheduler serve
```

T2 使用独立的 `submitter-connectivity.md` prompt，只允许调用一次 `create_proposal`；不会确认修订、
编译 Task 或调度 GPU，因此 **Worker 不需要启动**。测试从 Ground Truth 验证三件事：恰好一个
带当前 run marker 的 Proposal、其状态仍是 `AWAITING_CONFIRMATION`、没有任何绑定 Task。
`curl -sk https://127.0.0.1:8443/health` 能返回成功即可；`workers` 可以是 `0`。

### T3 前置：Master 用 claude harness 起

T3 跑真实的四卡资格闭环，Master 内部的 Processor/Reviewer **始终是 Claude**，与被测试的
Submitter harness 无关——`--harness` 只决定谁写 Proposal，不影响 Master 怎么审。所以 T3
必须把 Master 切回 `HARNESS_MODE=claude`：

```bash
export AGENT_SCHEDULER_STATE_ROOT=/public/share/agent-scheduler-mvp
export AGENT_SCHEDULER_PROFILE=qualification
export AGENT_SCHEDULER_HARNESS_MODE=claude
export AGENT_SCHEDULER_WORKER_MODE=remote
uv run agent-scheduler serve     # 终端 1，root
uv run agent-scheduler worker    # 终端 2，root
```

确认 `curl -sk https://127.0.0.1:8443/health` 的 `workers` 为 `1`、`integrity` 为 `valid`
后再进行 T3。

### 已知的跨 harness 限制

`run_submitter_agent()`（T3 走的驱动函数）在读取任何 `--harness` 参数之前，会先检查进程
自身环境里是否有 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`——这条检查是 Master 的
Processor/Reviewer 需要真实 Claude 凭据留下的，与 `--harness` 无关，**目前对全部四个
harness 一视同仁**。也就是说：即便 `--harness codex` 时 Submitter 本身用的是
`OPENAI_API_KEY`，运行 `agent-scheduler qualify --harness codex` 的终端仍然需要设好
Anthropic 凭据，否则会在还没读 `--harness` 之前就返回
`BLOCKED_QUALIFICATION`，理由写着"required for real Claude roles"——这个理由在
非 claude harness 下依然准确（Master 那头真的需要），只是措辞容易让人误以为是
Submitter 自己需要。同样，`tests/test_real_qualification.py` 里驱动 T3 的代码目前调用
`load_runtime()`，这个函数需要读 `secrets/`（root-only，`0600`）——跑 T3 目前仍需要
能读到这些身份文件的账号，与本项目其余部分「Submitter 应该是非 root 的 `zz_chentian`」
的方向不完全一致，是已知的、尚未收敛的差距。

## Claude ​Code

**一次性安装：** 无。

**凭据：** `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`（父进程环境提供）。先做一条
不带 MCP 的独立 smoke，排除 Claude ​Code 自身认证问题：

```bash
claude --print --no-session-persistence --setting-sources "" "Only reply: OK"
```

**T2：**

```bash
RUN_REAL_CLAUDE=1 uv run pytest tests/test_real_onboarding.py -m real_claude -v
```

**T3：**

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CLAUDE=1 \
  uv run pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[claude-RUN_REAL_CLAUDE]' \
  -v
```

这是四个 harness 里跑得最久的一条历史路径——`docs/qualification-status.md` 已经有一次
`COMPLETED` 的真实证据，可以直接对照那次的产物结构。

**已知点：** 非交互调用用 `--print --no-session-persistence --disable-slash-commands
--setting-sources "" --permission-mode dontAsk`，不需要任何危险 bypass flag——Claude ​Code
的 `dontAsk` 权限模式本身就是为非交互场景设计的。

## Codex CLI

**一次性安装：** 无，但需要先登录或提供凭据。

**凭据：** `OPENAI_API_KEY` + `OPENAI_BASE_URL`（若走自建/代理网关）。本机曾观察到
`codex login status` 显示 `Not logged in`——如果 T2 在这一步失败，先确认这条路径本身能用：

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='...'
codex exec --json "Only reply: OK"
```

必须 `export`，不要只把变量写在 smoke 命令前：T2/T3 会在新的 codex 子进程里读取父进程
环境，命令级临时赋值在 smoke 结束后就消失。跑通再进 T2/T3；跑不通说明问题在 codex
自己的认证，不在本项目的接入代码。

**T2：**

```bash
RUN_REAL_CODEX=1 uv run pytest tests/test_real_onboarding.py -m real_codex -v
```

**T3：**

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CODEX=1 \
  uv run pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[codex-RUN_REAL_CODEX]' \
  -v
```

**已知点：** 非交互调用带 `--dangerously-bypass-approvals-and-sandbox`——这不是本项目放松
了什么权限，而是 codex 的默认沙箱/审批流程会在没有 TTY 应答的子进程里永久挂起，必须绕过
才能作为自动化 Submitter 跑起来。这个 flag 只出现在 codex 分支
（`src/agent_scheduler/adapters/submitter.py` 里 `elif harness == "codex":` 块内），不会
泄漏到其他三家。

## pi

**一次性安装：**

```bash
pi install npm:pi-mcp-adapter
```

不装这一步，pi 根本读不到 `.mcp.json`，T2/T3 会在建 Proposal 那一步就失败——这不是
本项目代码的问题，是 pi 侧缺组件。

**凭据与模型：** pi 默认 provider 是 `google`；T2/T3 不能依赖这个隐藏默认值。本项目读取
`AGENT_SCHEDULER_PI_PROVIDER` 和 `AGENT_SCHEDULER_PI_MODEL`，并在自动 invocation 中转换成
pi 的 `--provider`/`--model` 参数。两者必须同时设置：

```bash
pi --list-models anthropic
export AGENT_SCHEDULER_PI_PROVIDER='anthropic'
export AGENT_SCHEDULER_PI_MODEL='<上一步列出的完整模型 ID>'
pi --provider "$AGENT_SCHEDULER_PI_PROVIDER" --model "$AGENT_SCHEDULER_PI_MODEL" \
  --print --no-session "Only reply: OK"
```

把占位符替换成 `--list-models` 实际列出的完整模型 ID。只设 provider 或只设 model 时，
测试夹具会在启动 pi 前直接报 `OnboardingError`，不会悄悄回退到默认 provider。

**T2：**

```bash
RUN_REAL_PI=1 uv run pytest tests/test_real_onboarding.py -m real_pi -v
```

**T3：**

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_PI=1 \
  uv run pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[pi-RUN_REAL_PI]' \
  -v
```

**已知点：**
- 不配 `directTools` 时，pi-mcp-adapter 把 12 个工具收敛成一个代理工具
  `mcp({tool: ..., args: ...})`——本项目生成的 `.mcp.json` 已经带上 `directTools`，所以
  12 个工具会以原生形态出现，不会卡在这一层。
- `PI_CODING_AGENT_DIR` 被重定向到每轮独立目录以实现隔离，这会同时隐藏默认目录中的凭据和
  已安装扩展。本项目在重定向前记住源 agent dir：`_seed_pi_agent_dir()` 把
  `auth.json`/`models-store.json` 复制进隔离目录，pi argv 再用
  `--extension <源目录>/npm/node_modules/pi-mcp-adapter/index.ts` 显式加载 adapter。
  如果源文件或 adapter 本身缺失（例如从未跑过 `pi auth`，或没执行上面的 `pi install`），
  T2/T3 仍会失败——先在正常的 pi agent dir 下配好凭据并安装扩展，再跑测试。

## dsh

**一次性安装：**

```bash
dsh plugin --profile headless add dsh-mcp-bridge
```

`dsh-mcp-bridge` 是第三方包（发布者 Edge-Echo）；它实际安装的是官方
`@deepseek-ai/dsh-mcp-client` 插件的示例配置，不是一个独立的竞争实现——如果不想依赖这个
第三方包，可以直接依赖 `@deepseek-ai/dsh-mcp-client` 并自己写同样的 cordis patch 条目。

**凭据：** dsh headless profile 默认用 `deepseek-official/deepseek-v4-flash`，需要
`DEEPSEEK_API_KEY`。先做一条不带 MCP 的独立 smoke：

```bash
dsh --profile headless "Only reply: OK"
```

**T2：**

```bash
RUN_REAL_DSH=1 uv run pytest tests/test_real_onboarding.py -m real_dsh -v
```

**T3：**

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_DSH=1 \
  uv run pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[dsh-RUN_REAL_DSH]' \
  -v
```

**已知点：** headless profile 的沙箱策略默认 `sandbox: workspace-write`、
`approval: ask`——非交互调用如果保留默认值会在第一次工具调用时卡死等待人工批准，永远不会
返回。本项目生成的调用会显式设置 `DSH_PERMISSION_MODE=danger-full-access`
（`src/agent_scheduler/adapters/submitter.py` 里 `else:` 分支，即 dsh 分支），把
`approval` 变成 `never`，只影响这一次 dsh 调用的环境变量，不改 `$DSH_HOME` 下任何持久配置。

## 排障顺序

四个 harness 的 T2 全部失败时，从下往上排查，不要跳步：

1. **`curl -sk https://127.0.0.1:8443/health`** 通不通——T2 只需要 Master；T3 还要求
   `workers` 是 `1`。先按当前测试层级判断，不要为了 T2 多启动 Worker。
2. **该 harness 能不能脱离本项目独立跑通一句话**（上面每节给的探测命令）——认证问题
   与本项目接入代码无关，修好这一步再往下。
3. **T2 本身**——如果前两步都通，T2 失败说明是 `build_onboarding()`/
   `build_submitter_invocation()` 生成的配置有问题，读 `_created_proposal_for` 失败时
   打印的 `stdout`/`stderr` 尾部，那里有该 Agent 实际收到的报错。
4. **T3**——T2 通过之后才有意义去跑，T2 没通过就跑 T3 只是在更贵的地方复现同一个问题。

## 记录结果

真实资格证据（无论 `COMPLETED` 还是 `BLOCKED_QUALIFICATION`）记录在
[资格状态](qualification-status.md)，按 harness 分节：`run_id`、四个
`proposal_id`/`task_id`、终态、`verify_qualification` 的结论。**代码与 Fake 门禁通过不
代表真实资格完成**——这是本仓库一贯的规矩，四个 harness 都适用，不因为换了 Agent 就
放宽。
