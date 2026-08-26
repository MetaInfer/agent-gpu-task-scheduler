# 多 Agent Submitter 接入设计

让 Claude ​Code 之外的三个 Agent —— Codex CLI、pi、dsh —— 也能作为 Proposal 发起者接入
调度器，并各自产出一套可被 `verify_qualification()` 独立校验的 1/2/4/8 完整证据包。

## 1. 边界

**产品面**是我们真正交付给接入方的东西：MCP 工具面、知识包（skill）、控制面错误信息、
四份接入配置模板与文档。**测试面**只用来证明产品面可用：`qualify` 里的 Submitter 驱动
及其 harness seam 是夹具，不是架构的一部分。

Submitter Agent 不受我们控制。我们不能读它的 exit code，不能要求它按某种格式回传结果，
也不能假设它的运行环境。凡是设计中依赖「Submitter 配合做某件额外的事」的地方，都要
换成「我们自己从 Ground Truth 观察」。

**硬前提**：接入方必须同时安装通路（MCP）和知识（skill）。这一步由人工完成；不装通路
就没有工具可调，根本无法调用服务。不提供自动校验手段。

一次性全局安装是接入方责任，与 `uv sync` 同级：

| Agent | 一次性安装 |
| --- | --- |
| Claude ​Code | 无 |
| Codex CLI | 无 |
| pi | `pi install npm:pi-mcp-adapter` |
| dsh | `dsh plugin --profile headless add dsh-mcp-bridge` |

## 2. 知识层：一份 skill，四处可见

Skill 内容不变，规范位置从 `.claude/skills/submit-gpu-task/` 搬到
`.agents/skills/submit-gpu-task/`（[Agent Skills 标准](https://agentskills.io/specification)）。
`reference/proposal-template.md` 随之搬迁，SKILL.md 内的相对引用不受影响。

四家的可见方式：

| Agent | 机制 |
| --- | --- |
| Codex CLI | 原生读 `.agents/skills`（二进制含 `.agents`、`skills/extraRoots/set`、`SKILL.md` 契约文本） |
| pi | 原生读项目 `.agents/skills/`（`docs/skills.md` 明载） |
| Claude ​Code | `.claude/skills/submit-gpu-task` 改为指向 `../../.agents/skills/submit-gpu-task` 的 symlink |
| dsh | `dsh-skill-filesystem` 的 roots 经 `--patch` overlay 指向 `.agents/skills` |

frontmatter 保持 `name` + `description` 两个字段。pi 明确放宽了「skill 名必须等于父目录名」
这条标准约束，理由正是跨 harness 共享目录；此处目录名与 `name` 本就一致，三家校验均可通过。

`.gitattributes` 已有 `* text=auto eol=lf`，symlink 不受文本转换影响，无需新增规则。

## 3. 接入层：四份配置，同一个 server

四份配置最终都指向同一条命令，服务端没有任何 agent 专属分支：

```
uv run agent-scheduler mcp --base-url https://127.0.0.1:8443 --username zz_chentian
```

环境变量 `AGENT_SCHEDULER_STATE_ROOT` 必须与 Master 一致——Adapter 用
`<state-root>/tls/certificate.pem` 作 CA。

### Claude ​Code

沿用现有 `config/submitter-mcp.example.json` → `.mcp.json`，或 `claude mcp add`。不改动。

### pi

`pi-mcp-adapter` 直接读 `.mcp.json`，**与 Claude ​Code 共用同一份文件**，无需第二份配置。

默认情况下 adapter 把 MCP 收敛成单个代理工具（`mcp({tool: "create_proposal", args: {...}})`）
以节省 context。这多一层转述就多一层出错机会，因此文档推荐在 `.pi/mcp.json` 中为
`submitter` 配置 `directTools`，把 12 个工具提升为原生形态。

pi 默认 provider 是 `google`，接入文档需说明用 `--provider` / `--model` 显式指定。

### Codex CLI

用 `-c` 覆盖而非 `codex mcp add`，因为前者不写 `~/.codex/config.toml`：

```
codex exec \
  -c mcp_servers.submitter.command=/path/to/uv \
  -c 'mcp_servers.submitter.args=["run","agent-scheduler","mcp","--base-url","https://127.0.0.1:8443","--username","zz_chentian"]' \
  -c 'mcp_servers.submitter.cwd="/public/share/fh/agent-gpu-task-scheduler"' \
  -c 'mcp_servers.submitter.env={AGENT_SCHEDULER_STATE_ROOT="/public/share/agent-scheduler-mvp"}'
```

### dsh

`dsh plugin --profile headless add dsh-mcp-bridge` 一次性安装，随后用 `--patch <overlay>`
声明 server，不改 `$DSH_HOME/profiles/headless/cordis.patch.yml`。

`dsh-mcp-bridge` 是第三方包（发布者 Edge-Echo），其依赖 `@deepseek-ai/dsh-mcp-client`
才是官方实现。文档需注明这一点，并把直接使用官方 client 列为备选。

### 交付物

- `config/` 下新增 codex、dsh 的配置模板（pi 复用 `.mcp.json`，只需 `directTools` 片段）
- `docs/submitting-from-a-claude-session.md` 扩写为覆盖四家的接入文档，并把「MCP 必须 /
  Skill 强烈建议」的表改为两者皆必须

## 4. 服务端：错误可教

`422 INVALID_PROPOSAL` 当前只回「内容不合契约」。改为指出**哪一节或哪个字段**不合、
期望什么形态。

范围克制：skill 已是硬前提，这一层只服务「装了 skill 但写歪了」的情况，让接入方一次改对
而不是猜多次。不做完整的自纠正指南。已知最高频的两类是 argv 不是两个位置参数、以及
`/data` ↔ `/public/share` 的 bind mount 声明缺失，优先覆盖这两类。

## 5. 夹具：`qualify --harness {claude,codex,pi,dsh}`

### Seam

`SubmitterHarness` protocol，四个实现只负责构造 argv 与 env。其余流程——本地门禁、
Master profile 校验、每轮配置生成、审计写入、证据校验——全部共用。

`prompts/submitter.md` 一律折进 prompt 正文，不使用任何 CLI 的 system-prompt flag：
codex 和 dsh 都没有这个能力，折叠后这一轴的差异消失，四家共用同一份 prompt。

### 结果获取：驱动侧重建

**不向 Agent 索取结果。** CLI 退出后，驱动按 `Qualification Run: <run_id>` 扫描事件存储，
找出正文含该标记且状态为 `COMPILED` 的 Proposal，从其 Task 反推 `items`。

理由：四个 CLI 差异最大的一轴恰是结构化输出（claude `--json-schema`、codex
`--output-schema`、pi 仅 `--mode json` 无 schema、dsh 无任何机制）。而
`QualificationResult` 本就不是可信输入——它只是一组指针，`verify_qualification()`
拿到后仍要对着签名证据图重新验一遍。既然内容不被信任，就没有理由为「让 Agent 把指针
念出来」承担四套解析路径。`Qualification Run: <run_id>` 这个绑定现在已经存在
（`_verify_item()` 正在检查），重建等于把已有绑定同时用作发现机制，不新增任何契约。

代价是丢掉 Agent 自述的 `BLOCKED_QUALIFICATION` 与文字理由。这个损失可接受甚至是正收益：
驱动观察到「只凑齐 3 种卡数」比 Agent 自称失败更准确，也能识别 Agent 自称成功但证据不全
的情况。`status` 由重建结果判定——凑齐 1/2/4/8 且全部 `COMPLETED` 为 `COMPLETED`，
否则 `BLOCKED_QUALIFICATION`，`reason` 描述实际缺口。

### 隔离

每轮运行的配置生成到 `<state-root>/qualification/<run_id>/`。**不修改任何用户 dotfile**
（`~/.codex/config.toml`、`~/.pi/agent/settings.json`、`$DSH_HOME/profiles/*/cordis.patch.yml`）。
这与仓库现在不提交生效版 `.mcp.json` 是同一条原则。

### 证据校验调整

`verify_qualification()` 中「恰好一条 `role == "submitter"` 的 harness 记录」这一条
改为按 harness 标识匹配：审计记录增加 `harness` 字段（`claude` / `codex` / `pi` / `dsh`），
校验时要求当前 run 下恰好一条对应 harness 的 submitter 记录。其余校验逻辑不变——它们
校验的是签名证据图，与 Submitter 是谁无关。

### CLI

`qualify` 新增 `--harness`，默认 `claude` 以保持现有行为。

## 6. 测试

### T1 · 契约层（默认门禁，零成本）

- 四份配置生成的结构正确性：指向真实存在的可执行文件、state-root 一致、JSON/TOML/YAML 合法
- skill frontmatter 对三家发现规则均合法，且位于各 agent 的预期发现路径
- 驱动侧重建逻辑：给定构造的事件存储，能正确反推 items、识别缺口、判定 status
- `verify_qualification()` 的 harness 标识匹配

### T2 · 通路层（opt-in，少量 token）

新增 marker `real_codex` / `real_pi` / `real_dsh`，与现有 `real_claude` 同级，各自由
`RUN_REAL_CODEX=1` 等环境变量 opt-in。

真实启动 agent，断言 12 个 submitter 工具**可达**并能成功创建一个 Proposal。Master 跑
`AGENT_SCHEDULER_HARNESS_MODE=fake`，不碰 GPU、不产生 Reviewer 费用。

「可达」而非「原生可见」：pi 在未配 `directTools` 时，12 个工具藏在 `mcp()` 代理工具之后，
断言原生工具名会误判为失败。统一的判据是 Proposal 确实被创建出来——它同时覆盖了工具发现、
参数传递和控制面连通三件事，且对四家的工具暴露形态无假设。

### T3 · 端到端层

`RUN_FULL_QUALIFICATION=1` + `--harness <name>`，每个 agent 完整跑 1/2/4/8 并通过
`verify_qualification()`。复用容器严格串行，四个 agent 共 12 个真实 Task，是整个计划中
最长的一段。

## 7. 实现期需验证的前提

以下三条不阻塞设计，但实现时必须先验证，失败则该 agent 的 T2/T3 记为 `BLOCKED`：

1. **Codex 认证**：当前 `codex login status` 为 `Not logged in`。需确认
   `OPENAI_API_KEY` + `OPENAI_BASE_URL` 这条路在 `codex exec` 下可用。
2. **pi provider**：默认 `google`，需确认用环境中已有的哪一家凭据（Anthropic / OpenAI /
   DeepSeek）以及对应的 `--provider` / `--model` 取值。
3. **dsh 沙箱**：headless profile 的 `sandbox-policy` 默认 `workspace-write` 且 approval
   为 `ask`。非交互运行需 `DSH_PERMISSION_MODE=danger-full-access` 才能 `approval: never`；
   需确认该设置下 MCP 工具调用不被拦截。
4. **pi 的每轮隔离**：pi 从项目 `.mcp.json` 或 `<Pi agent dir>/mcp.json` 读配置。夹具要
   做到不写仓库文件，需用 `PI_CODING_AGENT_DIR` 指向 `<state-root>/qualification/<run_id>/`，
   但 pi 的 `auth.json` 也位于 agent dir 下，改指后可能丢失凭据。需验证并确定处理方式
   （复制凭据到每轮目录，或改用项目级 `.pi/mcp.json` 并在运行后清理）。

## 8. 不做

- 不为「Agent 没装 skill」的场景做设计（已排除）
- 不提供接入自检命令
- 不把 harness seam 做成生产抽象——它是测试夹具
- 不新增 MCP 工具（工具面维持 12 个）
- 不修改任何用户 dotfile
