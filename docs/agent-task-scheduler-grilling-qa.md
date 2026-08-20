# Agent Task Scheduler Grilling QA 决策记录

- 访谈日期：2026-08-19 至 2026-08-20
- 状态：已确认
- 对应规范：[Agent 驱动 GPU 任务调度框架详细规范](./agent-task-scheduler-spec.md)

## 阅读说明

本文保留需求访谈的 Q1–Q326 编号，并把当时简写为“接受”的回答展开成实际选择。它用于解释决策来源，不取代正式规范。

同一主题经过多轮修订时，后面的决定覆盖前面的决定。为了避免误用，早期答案会直接注明最终结论或指出被后续问题取代。正式实现始终以 spec 为准。

## 第一轮：产品根边界（Q1–Q13）

- **Q1 · 产品边界是什么？** — 只负责 Proposal 审核、确定性 Task 生成、任务调度和容器清理，不负责代码托管、镜像构建、实验追踪或产物管理。
- **Q2 · 谁提交 Proposal，Proposal 与 Task 用什么形式？** — 主要由外部 Agent 提交。Proposal 是相对宽松但信息完整的 Markdown；Task 是字段严格、信息完整的 JSON。
- **Q3 · 是否需要人类批准？** — 正常路径全自动，无人类批准；提供只读前端观察系统。特殊运维是旁路，不是正常流程必经步骤。
- **Q4 · Proposal 是什么对象？** — 它是 Markdown、结构化 Facts、多轮消息、不可变 revision 和审核记录组成的复合对象。Processor先和提交 Agent澄清；Reviewer打回后继续澄清；全部过程可追溯，超限可驳回。
- **Q5 · “确定性 Task”保证到哪一层？** — 保证相同已批准输入生成相同 Task描述和容器配置，不承诺 GPU计算结果完全可复现。
- **Q6 · MVP 支持哪些工作负载？** — 只支持有界、非交互的 Docker batch job；不支持 Notebook、SSH、常驻服务。后来增加最多两节点的分布式 batch。
- **Q7 · 底层执行形态是什么？** — 每台 GPU机器运行一个 Worker，Worker根据严格 Task调用 Docker CLI。MVP不接 Kubernetes或Slurm。
- **Q8 · 开发环境与 GPU 如何连接？** — 开发人员在无GPU环境开发，只能让 Agent通过MCP或REST提交Proposal。获批后Worker在GPU节点执行，并注入`HIP_VISIBLE_DEVICES`。
- **Q9 · 资源不足怎么办？** — 默认排队；所有任务初始同优先级；等待满1小时进入AGED高优先级，避免饥饿。
- **Q10 · 清理成功如何定义？** — 业务期望以容器成功停止为核心；最终协议为stop失败后kill，新建容器还要rm，无法清理则锁住资源并进入`CLEANUP_FAILED`。
- **Q11 · 是否处理控制面重启、网络分区和自动重试？** — 不承诺崩溃或网络分区自动恢复。任务不自动重试；需要重试时由提交 Agent创建关联的新Proposal。
- **Q12 · MVP 规模是多少？** — 两台机器、每台8张卡，共16张K100_AI；面向小团队内部测试，不设并发SLO。
- **Q13 · Spec 的用途和覆盖范围？** — 作为可进入工程设计评审的详细规范，包含非目标、威胁模型、状态机、API、数据模型、调度、部署、可观测性、测试和分期范围。

## 第二轮：Agent 分工与容器模型（Q14–Q30）

- **Q14 · Processor 与 Reviewer 如何分工？** — 两个独立角色。Processor负责澄清和生成revision，无批准权；Reviewer只能批准、要求修改或驳回，不能修改Proposal。
- **Q15 · 多轮交互协议是什么？** — REST是规范异步接口，MCP是薄封装。提交Agent通过事件游标轮询并回复，不依赖webhook。
- **Q16 · 提交主体如何标识？** — 早期讨论principal_id，最终MVP简化为请求声明的`username`，由本地白名单验证其是否允许使用；不提供真实身份认证。
- **Q17 · Markdown 有多宽松？** — 使用固定必填标题，标题下正文完全自由。
- **Q18 · 是否维护内部结构化中间态？** — 维护与revision绑定的ProposalFacts，作为Schema校验和Task编译输入。
- **Q19 · 谁判断 Proposal 完整？** — Processor基于System Prompt中的关键字段判断；Master仍使用ProposalFacts Schema保证所有Task必填字段存在。
- **Q20 · Reviewer 按什么规则审核？** — 语义风险和合理性由模型自由判断；明确系统能力边界由确定性Validator执行，Reviewer不可绕过。
- **Q21 · 审核打回如何修改？** — 每次修改创建不可变revision；终局驳回后旧Proposal不可复活，只能创建关联新Proposal。
- **Q22 · 对话轮次与超时？** — Processor最多8个往返，Reviewer最多打回3次，每轮等回复30分钟，总生命周期7天；超时和超限终局结束。
- **Q23 · Task 如何生成？** — 确定性Compiler从已校验Facts和Policy Snapshot生成canonical JSON，再做Schema校验、hash和签名；LLM不能直接输出可执行Task。
- **Q24 · 代码如何进入容器？** — 在Proposal阶段确定。MVP最终支持inline Bash或容器内绝对脚本路径；内容/路径及SHA-256冻结在Task。
- **Q25 · 容器生命周期模型？** — 指定白名单预建容器时使用`start/exec/stop`复用；未指定时按image新建，使用`create/start/exec/stop/rm`，任务后立即删除。
- **Q26 · GPU申请粒度和多机？** — 只支持整卡独占。每Unit最多8卡；最多两个Unit、两个节点、总计16卡。双机可使用不同命令。
- **Q27 · 工作负载是否可信？** — 默认可信，允许root、privileged、host network和明确宿主挂载；不挂Docker socket。复杂恶意规避不在MVP威胁模型中。
- **Q28 · 谁设置优先级？** — 提交者不能设置。MVP没有人工调优功能，只使用统一初始等级和1小时aging。
- **Q29 · 是否允许取消？** — 创建者可取消协商中Proposal、排队或运行Task；运行中取消触发stop/清理，终态为`CANCELLED`，除非清理失败。
- **Q30 · 前端能做什么？** — 只读展示系统全局状态，不提供批准、调优、取消或重调度按钮。

## 第三轮：安全承诺与执行语义（Q31–Q51）

- **Q31 · GPU“隔离”是否是安全隔离？** — 不是。系统是可信内部环境中的协调、调度和审计框架，不是多租户安全沙箱。
- **Q32 · 高危容器能力默认值？** — root、privileged和host network默认开启并显式写入Task；Docker socket最终明确不挂载。
- **Q33 · 宿主保护有硬底线吗？** — MVP主要依赖Reviewer自然语言判断，不建立完整宿主路径硬策略；高权限风险作为已知限制。
- **Q34 · 危险命令如何检测？** — Reviewer自然语言判断明显危险命令；混淆写法、脚本内部行为等复杂规避不处理，也不宣称可靠检测恶意代码。
- **Q35 · username 在何时确定？** — 首次创建Proposal时由请求携带，Master检查本地username白名单并立即绑定；后续读取、回复、取消使用同一声明身份语义。
- **Q36 · 提交者凭证模型？** — 早期曾选择API Key；最终被Q323取代：提交API没有Token，只校验username白名单。Worker仍使用独立API Key。
- **Q37 · MVP代码交付方式？** — 支持Bash：inline脚本文本，或预建容器内的绝对脚本路径加SHA-256。
- **Q38 · 预建容器如何登记？** — 管理员维护`worker_id + container_name + username`本地白名单；Proposal可直接填写白名单中的精确容器名。
- **Q39 · 是否接受复用残留？** — 复用容器是stateful执行环境，只能串行使用；框架不清理文件或安装状态，残留由用户承担。
- **Q40 · 新建容器任务后怎么办？** — 无论成功失败都清理并删除，不保留24小时。
- **Q41 · 镜像如何保证确定性？** — Task记录image digest。新建按digest使用；复用容器核验实际digest。
- **Q42 · 命令如何表示？** — 支持结构化`executable + argv`和Bash联合类型；分setup/run/teardown三个阶段。
- **Q43 · Secret 如何注入？** — MVP不支持Secret注入、secret_ref或明文秘密字段。
- **Q44 · 双机任务的领域模型？** — 一个逻辑Task包含两个TaskUnit，每个Worker只执行分给自己的Unit。
- **Q45 · 双机是否要求同时获得资源？** — 使用gang scheduling；所有资源一次性预留成功后才启动。
- **Q46 · 一个Unit失败怎么办？** — 停止其他Unit，整体失败或取消；不自动重试。
- **Q47 · 成功终态叫什么，如何判断？** — 使用`COMPLETED`，不用`SUCCESS`。所有run前台运行且退出码为0只是必要条件，还需必需日志/output存在并清理成功。
- **Q48 · 是否有运行超时和强杀？** — Task总超时必填；结束、取消或超时先`docker stop --time 30`，失败后`docker kill`。
- **Q49 · 日志与产物责任？** — Framework stdout/stderr写NFS并保留30天；业务日志位置在Proposal阶段确定，框架返回路径但不管理内容。
- **Q50 · 人工优先级冲突如何解决？** — MVP取消人工优先级调整，正常路径和实现均只用自动aging。
- **Q51 · 哪部分可复现？** — LLM协商审核不确定；批准revision后的编译确定。保存Harness命令、Prompt、输入输出和工具记录用于审计。

## 第四轮：日志、Worker 和资源（Q52–Q79）

- **Q52 · Bash脚本如何进入Task？** — 支持inline文本，或容器内绝对路径加预期SHA-256；Worker执行前校验路径脚本hash。
- **Q53 · `COMPLETED`和失败如何区分？** — `COMPLETED`只表示全部成功条件成立；非零退出为`FAILED`，超时为`TIMED_OUT`，取消为`CANCELLED`。
- **Q54 · 是否区分框架日志和业务日志？** — 区分。框架捕获Docker执行stdout/stderr；业务日志由任务写共享存储。
- **Q55 · 业务日志写到哪里？** — 必须写入`/public/share/...`共享存储，不能只留在新建容器可写层。
- **Q56 · 提交者如何看业务日志？** — 自己通过共享存储权限读取；Master/API只返回路径。
- **Q57 · Proposal与Task是否一对一，双机怎么办？** — 一个批准revision最多生成一个逻辑Task；双机只是Task内两个Unit。重试生成关联新Proposal和新Task，不只重跑一个Unit。
- **Q58 · Task生成后可编辑吗？** — 不可编辑；任何业务变化都必须创建新Proposal/Task。
- **Q59 · Worker如何确认Task来源？** — Task使用SHA-256、RFC 8785 canonicalization和Ed25519签名；Worker校验Master公钥。
- **Q60 · 如何避免重复领取？** — Unit、assignment具有唯一ID，Master原子状态转换，Worker对同assignment幂等。
- **Q61 · Master和Worker谁主动通信？** — Worker主动建立WebSocket，Master沿连接主动push assignment。
- **Q62 · Worker上报哪些能力？** — hostname/IP、8张K100_AI、设备索引、hy-smi、Docker、镜像digest、白名单容器、Task和占用。
- **Q63 · 心跳语义？** — 每10秒心跳，连续3次缺失标记OFFLINE；不自动释放GPU或重调度。
- **Q64 · Worker可并发多少任务？** — 不同容器、不同GPU可并行；同一容器严格串行；除GPU约束外不设额外MVP并发上限。
- **Q65 · GPU Lease何时释放？** — 命令终止、stop成功、新建容器rm成功且终态上报后释放；stop/kill或rm失败则保留Lease。
- **Q66 · 正常完成是否也停止复用容器？** — 所有终态都必须stop。
- **Q67 · 为什么不用`docker run`？** — 为统一新建和复用状态机，新建采用`create/start/exec/stop/rm`；步骤虽多，但两类容器共享清理模型。
- **Q68 · 复用容器初始状态异常怎么办？** — 最低基线在Proposal/预检检查；占用则等待，digest不符或基线无效则前置条件失败，不自行修复。
- **Q69 · 不同username可共用预建容器吗？** — MVP白名单按username归属，默认不共享；所有使用仍严格串行。
- **Q70 · 新建镜像是否有限制？** — 只允许白名单repository/digest。
- **Q71 · Worker缺镜像怎么办？** — 镜像必须预装；缺少时Task为`BLOCKED`，管理员预装后重检，不自动pull。
- **Q72 · 提交者能指定物理机器吗？** — 通常只声明角色和需求，由Scheduler选；复用容器或本地资源依赖可隐式绑定required_worker_id。
- **Q73 · 双机rank/address/port谁生成？** — Master在Execution Plan生成`MASTER_ADDR/PORT/RANK/WORLD_SIZE`等并签名；Task本体仍不可变。
- **Q74 · teardown失败会阻止清理吗？** — 不会。teardown是best-effort，框架清理优先级更高。
- **Q75 · Aging公式是什么？** — MVP两级队列：等待达到1小时进入AGED，AGED优先，级内FIFO。
- **Q76 · 多卡任务可部分占卡吗？** — 禁止，Unit和gang都必须原子获得全部资源。
- **Q77 · Task资源字段到什么程度？** — Task字段完整并写`resource_type: K100_AI`与gpu_count；MVP不支持其他型号、显存或软件能力申请。
- **Q78 · 申请其他GPU怎么办？** — 在Proposal阶段直接拒绝，不生成Task。
- **Q79 · 提交Agent收到哪些事件？** — 澄清、批准、驳回、排队、启动、结束、失败、超时、取消等全部通过带递增sequence的REST/MCP事件轮询提供。

## 第五轮：控制协议和状态持久化（Q80–Q110）

- **Q80 · Worker控制协议？** — MVP使用WebSocket，不使用gRPC。
- **Q81 · Master是否高可用？** — 单Master、无HA。
- **Q82 · Worker如何部署？** — Python 3.10脚本进程，由管理员手动以root启动，不用systemd作为MVP硬要求。
- **Q83 · Worker是LLM Agent还是daemon？** — MVP为Claude Code Controller加确定性Driver；架构支持以后切换成纯deterministic controller。
- **Q84 · 理论/实际占用不一致怎么办？** — 理论空闲实际忙则GPU进入DRIFTED；理论忙实际空闲只告警，不自动杀任务；未分配占用不自动清理。
- **Q85 · 实际占用阈值？** — `hy-smi VRAM% >= 2%`即忙，不使用compute process作为主判据。
- **Q86 · K100_AI定义？** — 资源类型常量为K100_AI，监控工具`hy-smi`，可见设备变量`HIP_VISIBLE_DEVICES`，每节点8卡。
- **Q87 · Task是否仍写完整资源字段？** — 写完整`resource_type: K100_AI`和gpu_count；其他类型在Proposal阶段拒绝，因此不会污染Task。
- **Q88 · Aging具体队列？** — NORMAL与AGED两级，1小时晋级，AGED优先，级内按原入队时间FIFO。
- **Q89 · 大型AGED任务如何避免碎片饥饿？** — 队首大型AGED任务出现后停止投放妨碍其凑卡的小任务，允许卡暂时闲置。
- **Q90 · principal具体是什么，最终怎么简化？** — 原概念是稳定责任主体；MVP最终以username替代，预建容器白名单也绑定username。
- **Q91 · 新建容器名谁生成？** — Master在Execution Plan生成唯一名，提交者不能指定；同名存在时不复用。
- **Q92 · 容器内脚本hash何时校验？** — Worker在执行业务命令前用容器内`sha256sum`核验声明值。
- **Q93 · Proposal状态机？** — DRAFT、CLARIFYING、READY_FOR_REVIEW、IN_REVIEW、CHANGES_REQUESTED、APPROVED、TASK_COMPILED，以及REJECTED、EXPIRED、CANCELLED、COMPILE_FAILED、PROCESSING_ERROR。
- **Q94 · Task状态机？** — CREATED、BLOCKED、QUEUED、RESERVED、DISPATCHED、STARTING、RUNNING、COMPLETED，以及UNSCHEDULABLE、PRECONDITION_FAILED、FAILED、TIMED_OUT、CANCELLED、CLEANUP_FAILED、RECONCILIATION_REQUIRED。
- **Q95 · 多Unit状态如何聚合？** — 固定优先级：清理失败最高，其次取消、超时、其他失败；全部Unit成功才COMPLETED。
- **Q96 · UNSCHEDULABLE与BLOCKED如何分？** — 不支持硬件在Proposal拒绝；暂缺预装镜像为可恢复BLOCKED；永久失效才UNSCHEDULABLE。
- **Q97 · 是否保留Attempt概念？** — MVP不做多attempt，但保留唯一`execution_id`用于关联两Unit、日志、容器和assignment。
- **Q98 · 取消和完成竞态？** — 以Master事件实际落盘顺序决定，迟到事件只审计不覆盖终态。
- **Q99 · Proposal超时后能否恢复？** — 不能，只能创建关联新Proposal。
- **Q100 · 如何计一个往返？** — Processor一条消息和提交Agent一条有效回复构成一个往返，不按问题数量计。
- **Q101 · 每轮都生成完整Markdown吗？** — 是，每轮产生完整不可变revision并记录消息来源。
- **Q102 · 送审前是否需要提交Agent确认？** — 必须显式`confirm_revision(revision_id)`。
- **Q103 · Reviewer看到什么上下文？** — 默认看到最终revision和审核材料，完整历史可通过只读MCP工具检索。
- **Q104 · Reviewer输出是否结构化？** — 必须符合严格JSON Schema。
- **Q105 · Processor输出是否结构化？** — 必须包含message、完整Markdown、Facts、missing_information和ready_for_review。
- **Q106 · LLM调用失败？** — 同一次调用最多自动重试3次；仍失败进入PROCESSING_ERROR，不计入业务轮次。
- **Q107 · 使用什么数据库？** — 不使用数据库；NFS文件系统是Ground Truth。
- **Q108 · Task如何存储？** — 签名canonical `task.json`保存在NFS，事件文件记录生命周期；不再有数据库副本。
- **Q109 · 审计是否可修改？** — append-only并永久保留；当前状态快照可更新，但历史事件不可改删。
- **Q110 · 数据保留？** — Proposal、消息、revision、审核、Task、Harness记录和审计永久；Framework日志30天；业务日志由外部策略管理。

## 第六轮：身份简化、NFS 与 Worker Agent（Q111–Q142）

- **Q111 · Principal/容器所有权最终如何简化？** — 使用username本地白名单，容器条目绑定username，不实现复杂Principal系统。
- **Q112 · 管理员是否建模为Principal？** — MVP前端没有管理员操作；旁路Admin CLI记录操作人和理由，但不建设完整角色系统。
- **Q113 · username如何创建？** — 管理员提前编辑本地YAML白名单，不提供在线注册。
- **Q114 · Worker LLM承担什么职责？** — 仅编排受限生命周期工具，不修改Task或自创恢复动作。
- **Q115 · Worker LLM能用任意Shell吗？** — 不能；只开放确定性MCP工具。业务Bash只能通过Driver的执行工具运行。
- **Q116 · Worker能修正Task吗？** — 不能；前置条件不符只报告失败。
- **Q117 · Worker LLM失败时任务怎么办？** — Python supervisor持有生命周期并进入清理；Task失败原因为WORKER_AGENT_ERROR。
- **Q118 · Worker Harness审计？** — 保存命令、Prompt、输入输出和工具调用；Framework命令日志30天。
- **Q119 · 以后切daemon是否兼容？** — Claude和deterministic controller实现同一协议与工具接口，Master无感知。
- **Q120 · root Worker风险？** — MVP将整机被控制列为已知风险；限制LLM工具，不给任意root Shell。
- **Q121 · WebSocket建连方向？** — Worker出站连接，Master沿连接push。
- **Q122 · WebSocket安全？** — 使用WSS；Worker用独立API Key认证，验证Master TLS和Task签名。提交者API最终无Token。
- **Q123 · Worker是否独立凭证？** — 两台Worker各有可独立撤销的API Key。
- **Q124 · 消息可靠性？** — message_id、assignment_id、sequence、先持久化后push、Worker登记后ACK、重复assignment幂等。
- **Q125 · ACK超时？** — 30秒未ACK先查询Worker，确认未登记后才释放并回队。
- **Q126 · 共享存储类型？** — NFS；Master与Worker A同机，Worker B远端挂载；宿主共享根为`/public/share`。
- **Q127 · 谁写权威状态？** — 只有Master写Proposal/Task/调度/审计；Worker通过WebSocket上报，直接写Framework日志。
- **Q128 · 文件系统状态模型？** — 不可变对象、append-only事件、可重建快照。
- **Q129 · 是否考虑写一半崩溃？** — 不承诺崩溃一致性；必须支持Graceful shutdown排空写队列。快照可用临时文件+rename减少风险。
- **Q130 · 如何避免双Master？** — MVP只依赖默认端口冲突避免同机重复启动，不处理跨机误启动。
- **Q131 · queue/lease能否重建？** — 必须能从对象和事件重建，快照只做加速。
- **Q132 · Master重启最小恢复？** — 恢复历史与未下发队列；不明确的DISPATCHED/RUNNING进入RECONCILIATION_REQUIRED，不自动重跑/释放。
- **Q133 · Task唯一权威内容？** — NFS签名canonical Task是唯一权威载荷，事件保存生命周期。
- **Q134 · ID格式？** — 类型前缀加UUIDv7。
- **Q135 · 是否需要execution_id？** — 保留唯一execution_id，即使MVP不自动重试。
- **Q136 · 取消竞态？** — Master落盘顺序决胜，终态不可被迟到事件覆盖。
- **Q137 · 永久保留哪些数据？** — Proposal、对话、revision、审核、Task、Worker Harness记录和审计永久保存。
- **Q138 · 用户能删除历史吗？** — MVP不支持删除，只允许取消。
- **Q139 · 是否自动扫描误写Secret？** — MVP不实现自动秘密扫描；Secret管理和脱敏列为非目标。
- **Q140 · K100_AI监控适配？** — 使用`hy-smi`和`HIP_VISIBLE_DEVICES`，配置资源类型、8卡和2%阈值。
- **Q141 · 2%口径？** — 直接读取`hy-smi`的VRAM%列，`>=2%`判忙；同时上报原始行和DCU%。
- **Q142 · 成功状态拼写？** — 使用`COMPLETED`。

## 第七轮：外部接口、Proposal 与 Task Schema（Q143–Q182）

- **Q143 · 提交API Key从哪里来？** — 最终被Q323修订：提交API不使用API Key，只检查username白名单。
- **Q144 · 前端需要登录吗？** — 不需要。
- **Q145 · MVP前端管理员能力？** — 不实现任何管理员写功能。
- **Q146 · 管理操作是否要求理由？** — 前端无管理操作；Admin CLI改变状态时仍要求理由和审计。
- **Q147 · 固定拓扑确认？** — 节点A为Master+Worker A+8卡，节点B为Worker B+8卡，共用NFS。
- **Q148 · NFS路径映射？** — 宿主根`/public/share`，实际业务目录为其子目录；容器内挂载点由Proposal协商。
- **Q149 · NFS未挂载如何处理？** — MVP把NFS稳定作为部署假设，不做恢复；执行前仍做廉价目录/可写性预检。
- **Q150 · Master Graceful shutdown？** — DRAINING、拒绝新Proposal、停调度、排空写入，不杀运行任务，最多60秒退出，重启后reconcile。
- **Q151 · Worker Graceful shutdown？** — 有运行任务时默认拒绝；强制关闭必须先取消并清理。
- **Q152 · Worker是否无状态？** — 最终定义为任务间无状态、无权威持久状态；执行中内存有状态；异常崩溃恢复不支持。
- **Q153 · REST/MCP/前端关系？** — REST是权威层，MCP和前端均调用REST，不直接写NFS。
- **Q154 · Proposal API最小集合？** — 创建、查询、revision、事件、回复、确认、取消、关联Task、Task状态和取消。
- **Q155 · 事件获取方式？** — `after_sequence`普通轮询；MCP封装wait工具，不做SSE/webhook。
- **Q156 · 初稿可否不完整？** — 可以，但固定标题都要存在，未知写TBD；送审前清零TBD。
- **Q157 · Proposal固定章节？** — Identity、Objective、Success Criteria、Workload and Code、Container、Resources、Commands、Inputs and Mounts、Environment、Networking and Privileges、Timeout and Cleanup、Framework Logs、Business Logs and Outputs、Multi-node Coordination、Risks and Notes。
- **Q158 · Markdown username是否权威？** — 不是；Master使用请求声明且白名单通过的username覆盖/校验。
- **Q159 · Processor误判完整怎么办？** — Master的ProposalFacts Schema失败后回到CLARIFYING继续补齐。
- **Q160 · Container章节必须确定什么？** — create/reuse、image/digest、复用worker/container、privilege/network/mount/workdir等全部执行信息。
- **Q161 · tag何时解析digest？** — Proposal阶段由Master根据白名单和Worker已装镜像解析，提交Agent确认冻结值。
- **Q162 · 是否允许非NFS宿主挂载？** — 允许Proposal显式声明并由Reviewer判断；业务日志源必须位于`/public/share/`。
- **Q163 · Task顶层字段？** — Schema版本、各对象ID、username、时间、metadata、K100_AI、execution_id、units、scheduling、timeout、logging、policy snapshot、hash和签名。
- **Q164 · Task保存自然语言目标吗？** — 保存用于审计/展示，但Worker不能据此改变执行。
- **Q165 · Unit数量？** — 1或2，每Unit最多8卡，总计最多16卡。
- **Q166 · Unit完整字段？** — role、gpu_count、worker约束、container/image、mounts、workdir、env、setup/run/teardown、business logs和outputs。
- **Q167 · required_worker_id何时使用？** — 默认null；依赖复用容器或节点本地资源时冻结指定Worker。
- **Q168 · Bash两种表示？** — inline内容+hash，或容器绝对路径+hash。
- **Q169 · exec和Bash能否并存？** — 都支持，作为命令联合类型；每Unit恰好一个run。
- **Q170 · 普通环境变量和Secret？** — 普通变量全部冻结；MVP禁止Secret，但不做自动扫描。
- **Q171 · 默认高权限字段？** — Task显式写root、privileged、host network；最终`mount_docker_socket=false`。
- **Q172 · HIP_VISIBLE_DEVICES写在哪？** — Task只写数量，Execution Plan写物理卡，Worker运行时覆盖注入。
- **Q173 · Task与Execution Plan边界？** — Task冻结需求并签名；Plan冻结Worker/GPU/container/rank/address/port并签名。
- **Q174 · 业务日志字段？** — 每Unit至少一个NFS业务日志路径，路径应含task_id/unit_id。
- **Q175 · 日志缺失是否还能完成？** — 不能；必需日志缺失导致FAILED。
- **Q176 · Framework日志位置？** — NFS下按task/unit/execution分stdout/stderr文件，保留30天。
- **Q177 · 超时层级？** — 一个Task总超时；后续明确从首个setup开始，另有固定阶段超时。
- **Q178 · 双机启动窗口？** — 60秒，任一侧未进入run则停止另一侧并失败。
- **Q179 · 签名算法？** — RFC 8785 + SHA-256 + Ed25519。
- **Q180 · Worker如何取得Task？** — WebSocket发送ID、NFS URI、hash和签名；Worker从NFS读取校验。
- **Q181 · 执行前检查？** — 签名/Schema/约束/image/container/script/NFS/mount/GPU占用全部预检。
- **Q182 · Gang预检原子性？** — 两边都通过后才建立全部Lease和启动。

## 第八轮：LLM、调度、前端和实现（Q183–Q222）

- **Q183 · 提交认证模型？** — 早期选过团队共享Token，最终被Q323改为无Token、username白名单声明。
- **Q184 · Token生成保存？** — 最终不适用于提交API；Worker独立Key仍由管理员配置并只保存hash。
- **Q185 · API网络边界？** — 只部署可信内网，不暴露公网。
- **Q186 · 匿名前端可见范围？** — 可以展示全部非秘密Proposal、对话、命令、挂载、日志路径和系统状态。
- **Q187 · 前端是否完全无管理？** — 是，纯观察。
- **Q188 · Worker无状态最终模型？** — 无跨任务持久业务状态；执行中只在内存持有状态；崩溃恢复非目标。
- **Q189 · 断线事件缓冲？** — Framework日志直写NFS，小型状态事件内存最多10,000条，重连补报，溢出记录事件。
- **Q190 · 框架NFS根？** — `/public/share/agent-scheduler`；业务日志可位于其他`/public/share/...`子目录。
- **Q191 · 路径穿越？** — 业务日志宿主路径规范化后必须留在`/public/share/`，不允许`..`逃逸。
- **Q192 · NFS运行中失败？** — NFS稳定是部署假设；启动前检查目录可写，运行中故障不承诺自动恢复。
- **Q193 · Processor/Reviewer模型隔离？** — 可用同一基础Harness，但独立进程、独立Prompt、独立上下文。
- **Q194 · 具体Harness？** — MVP使用Claude Code，未来可换dsh、Codex等Adapter。
- **Q195 · LLM参数？** — 不由MVP管理具体temperature/token等；只规定进程超时和结构化输出。
- **Q196 · Prompt版本？** — Prompt放代码仓库版本化文件，并在调用记录保存内容hash。
- **Q197 · Processor工具？** — 只读资源、镜像、容器、Worker、路径和历史工具，不执行Docker。
- **Q198 · Reviewer工具？** — 同类只读事实工具，全部调用审计。
- **Q199 · 明确不支持的配置谁拒绝？** — Validator硬阻止，提交者坚持则Reviewer必须REJECT。
- **Q200 · 送审确认后能否改？** — 不能；打回后派生新revision并再次确认。
- **Q201 · Aging计时起点？** — Task首次进入QUEUED，BLOCKED/RESERVED/DISPATCHED不累计，回队保留累计值。
- **Q202 · 两级队列规则？** — 1小时AGED、AGED优先、级内FIFO、大任务资源保留。
- **Q203 · 是否调度CPU/内存？** — 不实际考虑，不进入集群装箱判断。
- **Q204 · CPU/内存默认字段？** — Task仍显式写`null`表示不限；shared memory也显式字段。
- **Q205 · GPU可分配条件？** — 无Lease、无本地Task、Worker在线、VRAM<2%、容器可用。
- **Q206 · hy-smi采样？** — 每10秒，样本30秒内有效，预留前即时采样。
- **Q207 · GPU ID？** — 直接使用hy-smi的DCU 0–7，Execution Plan记录，注入HIP_VISIBLE_DEVICES。
- **Q208 · Worker额外并发上限？** — 不设；GPU和容器锁自然限制。
- **Q209 · 最终Docker高权限参数？** — privileged、host network、root、Proposal挂载；明确不挂`/var/run/docker.sock`。
- **Q210 · 复用容器配置不一致？** — MVP不全面重配或二次比对，只检查最低基线、白名单和image digest。
- **Q211 · 新建容器删除保证？** — 所有终态都删除；删除失败进入CLEANUP_FAILED并锁资源。
- **Q212 · 复用容器最终状态？** — 所有终态stopped，文件状态保留。
- **Q213 · teardown失败影响成功吗？** — 不直接改变业务结果，记录warning并继续清理。
- **Q214 · Framework日志捕获范围？** — setup/run/teardown、Controller工具和Docker清理命令全部捕获并标阶段。
- **Q215 · 前端页面？** — 总览、Proposal/revision/review、Task/Unit、队列、GPU、容器、Lease、日志、审计。
- **Q216 · 前端实时方式？** — 每5秒REST轮询，不用浏览器WebSocket。
- **Q217 · 配置格式？** — YAML；敏感Worker Key/签名私钥/模型认证通过受保护环境或权限文件。
- **Q218 · 技术栈？** — Python 3.10、FastAPI、Pydantic/JSON Schema、WebSocket、简单HTML/JS。
- **Q219 · Docker SDK还是CLI？** — 直接调用Docker CLI，argv数组，不拼接shell字符串。
- **Q220 · Prometheus？** — MVP不接，只提供health/ready和JSON状态。
- **Q221 · 最低端到端验收？** — 单卡新建/复用、8卡、16卡gang、审核打回、硬拒绝、日志缺失、超时取消、hash不符、资源漂移、aging、断线幂等和cleanup失败全部覆盖。
- **Q222 · 非目标清单？** — 安全多租户、数据库/HA、崩溃恢复、自动重试、K8s/Slurm、非K100、切卡、交互服务、镜像构建/pull、Secret、产物管理、前端管理和Prometheus均排除。

## 第九轮：Claude Harness 和容器基线（Q223–Q258）

- **Q223 · Worker无持久状态确认？** — 执行中内存有状态，空闲无业务状态，不建本地持久journal，异常恢复非目标。
- **Q224 · Claude Code调用模式？** — 独立非交互`--print --bare --no-session-persistence --output-format stream-json --json-schema --strict-mcp-config`等价行为。
- **Q225 · 是否每轮全量回放历史？** — 不全量回放；使用Context Packet内联当前完整状态，旧历史按需MCP检索，保持可审计并改善上下文和缓存。
- **Q226 · Claude工具权限？** — 禁用非授权内置工具，只开放角色MCP；不使用dangerously-skip-permissions。
- **Q227 · Claude认证谁负责？** — 部署管理员预配置，框架不管理凭证生命周期。
- **Q228 · 是否记录Claude版本？** — 不要求；记录实际执行命令、工作目录、输入输出和退出码，未来可接其他Harness。
- **Q229 · Worker Controller决策边界？** — 只能选允许的生命周期工具，Driver决定参数、状态、超时和清理。
- **Q230 · LLM是否陪伴长任务？** — 不需要。Controller调用execute_assignment后由Python supervisor接管，Claude进程可退出。
- **Q231 · 是否支持纯daemon模式？** — 支持`controller_mode: claude|deterministic`，外部行为必须等价。
- **Q232 · 新建容器主进程？** — 覆盖默认CMD为idle常驻进程，业务全部通过docker exec。
- **Q233 · 预建容器基线？** — 可安全stop、start后idle常驻、提供bash、白名单、digest匹配、无并发任务。
- **Q234 · 复用容器配置漂移处理？** — 不修改配置，不做全面二次比对；最低基线、白名单和digest必须满足。
- **Q235 · inline Bash如何执行？** — 通过`docker exec -i ... /bin/bash -s --`从stdin传脚本。
- **Q236 · container_path Bash如何执行？** — 容器内sha256sum校验后`/bin/bash <path> <args>`。
- **Q237 · 阶段是否独立exec？** — 每条setup、run、每条teardown分别exec并记录日志/退出码。
- **Q238 · setup失败状态？** — `FAILED: SETUP_FAILED`，不执行run，仍best-effort teardown和清理。
- **Q239 · Worker覆盖哪些环境变量？** — HIP_VISIBLE_DEVICES、Task/Unit/Execution ID和全部分布式变量。
- **Q240 · 多机地址？** — Worker配置固定可互通内网IP，rank0 IP为MASTER_ADDR。
- **Q241 · 分布式端口？** — Master从配置范围分配、预检并Lease，写入Execution Plan。
- **Q242 · rank与启动顺序？** — Proposal定角色/命令，Master定rank；setup并行，run先rank0后其他rank，60秒窗口。
- **Q243 · Gang何时RUNNING？** — 所有Unit进入run前保持STARTING，全部进入后才RUNNING。
- **Q244 · rank0过早退出？** — 另一Unit启动前退出0也算gang启动失败。
- **Q245 · stop后显存残留？** — Task清理可成功、Lease释放，GPU转DRIFTED，不重新分配。
- **Q246 · DRIFTED何时恢复？** — 连续3次、每次约10秒VRAM<2%。
- **Q247 · Cleanup失败如何人工处理？** — 管理员登录节点修复，再通过Admin CLI reconcile/release并写理由。
- **Q248 · Admin CLI是否属于MVP？** — 属于，至少含查看阻塞资源、reconcile、release GPU、重载白名单和drain Master。
- **Q249 · MCP部署在哪里？** — 选B：每个提交Agent本地运行MCP Adapter，再调用Master REST。
- **Q250 · username如何传？** — MCP Adapter从环境变量固定读取，REST用X-Username；单次工具调用不能改。
- **Q251 · API幂等键？** — 创建Proposal和发送回复必须提供；同username同key返回原结果，内容冲突报错。
- **Q252 · API错误结构？** — 统一error_code、message、object_id、current_state、request_id、retryable。
- **Q253 · 30分钟计时刷新？** — 有效回复后重置；Processor和Harness重试时间不计。
- **Q254 · 历史事件游标？** — 永久保留，可从任意旧sequence继续读；终态只读。
- **Q255 · “公开所有权限”是什么意思？** — 公开全部只读数据，不开放匿名写API；前端不持有提交凭证（最终提交API也无Token，但前端不调用写接口）。
- **Q256 · 技术栈约束强度？** — Python3.10、FastAPI、Claude Code、Docker CLI、NFS、WebSocket是MVP MUST；内部类名/目录仅建议。
- **Q257 · Harness超时？** — Processor/Reviewer 10分钟，Worker Controller 5分钟，失败最多3次调用重试。
- **Q258 · 是否生成LLM执行总结？** — MVP不生成；结构化状态、退出码和日志路径足够。

## 第十轮：上下文、文件契约和完成语义（Q259–Q292）

- **Q259 · Context Packet策略？** — 当前完整状态内联，旧历史按需检索，不每轮回放全部原文。
- **Q260 · 历史检索工具？** — get_messages、get_revision、diff_revisions、get_review、search_proposal_history，调用与返回均审计。
- **Q261 · decision ledger是什么？** — 确定性索引，不替代原始消息、revision和review Ground Truth。
- **Q262 · Harness统一接口是否写成Skill？** — 不。使用Python HarnessAdapter、MCP tools和JSON Schema；Skill只可作为未来可选角色包装。
- **Q263 · Harness命令如何记录？** — 保存脱敏argv、工作目录、开始结束、退出码；Prompt单独存文件，不把凭证放命令行。
- **Q264 · Harness并发？** — Master默认最多4个进程。
- **Q265 · 同一Proposal并发？** — 严格串行，用Proposal级内存锁。
- **Q266 · 输入大小？** — 初始Markdown和单条回复各256KiB，不支持附件。
- **Q267 · 取消时Harness还在跑？** — 终止子进程，丢弃未落盘结果，Proposal进入CANCELLED。
- **Q268 · 轮次累计？** — Processor总共最多8往返；Reviewer总共最多4次审核（首次+3次打回）。
- **Q269 · Schema版本？** — ProposalFacts、Review、Task、Plan和WebSocket各自带schema_version；未知版本拒绝，不自动迁移。
- **Q270 · 时间格式？** — 持久时间UTC RFC3339，超时用单调时钟。
- **Q271 · 事件字段？** — event_id、对象内sequence、timestamp、object、event_type、actor、request_id、payload。
- **Q272 · Master写入并发？** — 单写入队列，状态事件落NFS后才返回API成功，Graceful shutdown排空。
- **Q273 · username白名单格式？** — YAML users列表，username安全字符；`enabled`字段保留但MVP只使用启用条目。
- **Q274 · 镜像/容器白名单格式？** — 镜像repository+digest；容器worker_id+name+username，精确匹配，不用通配符。
- **Q275 · 禁用传播？** — MVP不实现禁用/删除语义，只支持增加或修改；热加载只影响新对象。
- **Q276 · Admin CLI如何写状态？** — Master运行时通过localhost管理API，不直接改NFS；离线只做inspect/repair-index。
- **Q277 · Master启动恢复顺序？** — 配置/目录校验、读对象事件、重建索引队列Lease、标记reconciliation、开放接口、Worker比对、READY。
- **Q278 · Shutdown后的队列？** — 保持QUEUED和原aging时间，重启继续。
- **Q279 · Worker注册是否校验IP？** — 不做复杂固定IP白名单；只配置worker_id与独立API Key hash，动态上报能力。
- **Q280 · 哪些配置可热加载？** — username/image/container白名单可热加载；地址、NFS、端口、签名密钥需重启。
- **Q281 · COMPLETED业务含义？** — 所有run退出0、必需日志/output存在、清理成功；不保证自然语言业务目标达成。
- **Q282 · Success Criteria用途？** — Reviewer判断合理性和可验证性；Worker不解析业务日志判断指标。
- **Q283 · output缺失？** — 每个output标required；必需项缺失FAILED，可选项warning。
- **Q284 · setup副作用回滚？** — 不回滚，只teardown和容器清理；复用容器残留由用户承担。
- **Q285 · 总超时起点？** — 第一个Unit开始setup时，preflight不计，setup/gang/run计入。
- **Q286 · 阶段超时？** — setup每条10分钟、teardown每条5分钟、start 1分钟、stop grace 30秒、kill 1分钟、rm 1分钟，均进入Policy Snapshot。
- **Q287 · 阶段超时状态？** — setup/run超时为TIMED_OUT；teardown超时warning并继续清理。
- **Q288 · 哪些阶段可取消？** — QUEUED、RESERVED、DISPATCHED、STARTING、RUNNING；已启动Unit全部停止清理。
- **Q289 · Framework日志清理？** — Master每日删除终态且超过30天的Framework日志，不删元数据或业务日志。
- **Q290 · 前端日志加载？** — 默认尾部1000行，按byte offset继续。
- **Q291 · Spec语言与路径？** — 中文Markdown，最终路径`docs/agent-task-scheduler-spec.md`。
- **Q292 · 是否加图？** — 加Mermaid架构、Proposal状态、Task状态和双机时序/流程表达；正文/表格权威。

## 最终反向验收（Q293–Q322）

- **Q293 · MVP是否把Harness接口实现成Skill？** — 不；只实现程序化HarnessAdapter。角色Prompt文件版本化，未来可选Skill不能改变协议。
- **Q294 · 新Harness如何接入？** — 必须通过同一契约测试，领域状态机不得出现按harness分支。
- **Q295 · Worker认证最低模型？** — 两Worker独立Key，Master配置worker_id→hash，不校验来源IP，未知worker_id拒绝。
- **Q296 · 白名单热加载最终语义？** — 只支持增加/修改，只影响新Proposal/Task，不做禁用传播。
- **Q297 · Task被手工改怎么办？** — 手工篡改是MVP非目标，但签名校验仍保留；校验失败拒绝执行，不提供修复流程。
- **Q298 · Execution Plan被改怎么办？** — 同样签名校验，失败拒绝；Worker不重建Plan。
- **Q299 · 重复下发？** — Worker按assignment_id幂等，只回当前状态，不重复Claude/Docker执行。
- **Q300 · Master Graceful停机后任务完成？** — Worker继续写NFS日志并在内存保留终态，Master重启重连后补报。
- **Q301 · Master kill -9？** — 不自动恢复；不明确对象进入RECONCILIATION_REQUIRED，不重跑、不释放资源。
- **Q302 · Worker崩溃？** — Worker OFFLINE，保留Lease，不调度其GPU，人工处理。
- **Q303 · 长时间WebSocket断线？** — 任务继续、日志写NFS、Master不释放资源；分区期间取消不保证。
- **Q304 · 预留时发现VRAM 3%？** — 撤销整个gang预留，Task回QUEUED，GPU DRIFTED，不算业务失败。
- **Q305 · 双机只启动一边？** — 停止已启动侧，整体FAILED:GANG_START_TIMEOUT，不重试。
- **Q306 · 一边正常提前结束、另一边仍运行？** — 两边都曾RUNNING后允许等待另一边；全部0才完成。
- **Q307 · Reviewer错误批准硬非法配置？** — Validator/Compiler拒绝，Proposal COMPILE_FAILED或回澄清，Reviewer不能越过。
- **Q308 · Compiler内部异常可否重跑？** — 管理员可对相同revision/Facts/Policy原样重跑，记录每次编译尝试，最终仅一个有效Task。
- **Q309 · 已批准预建容器被删？** — 作为部署假设违反，不承诺自动修复；廉价预检可发现并阻止执行，需要新Proposal或人工处理。
- **Q310 · 预建容器启动即退出？** — 同属基线假设违反；最低预检/启动检查可阻止执行，不自动修复。
- **Q311 · Framework日志运行中NFS故障？** — NFS稳定是MVP部署假设，不设计自动恢复；启动前检查可写。
- **Q312 · run 0但required output缺失？** — FAILED:EXPECTED_OUTPUT_MISSING，仍返回已有日志和路径。
- **Q313 · stop成功但显存残留？** — Task可COMPLETED，GPU DRIFTED，连续3次低于2%后恢复。
- **Q314 · stop和kill都失败？** — 虽不做复杂恢复，保留最小fail-safe：CLEANUP_FAILED、Lease不释放、Admin CLI人工处理。
- **Q315 · 取消与自然完成同时到达？** — Master落盘顺序决胜，迟到事件只审计。
- **Q316 · 8卡AGED任务导致7卡闲置？** — 系统选择以短期吞吐量换取公平性，保留资源避免大型任务继续饥饿。
- **Q317 · 提交Token泄漏？** — 最终没有提交Token；任意调用方只需声明白名单username，真实认证不在MVP范围。
- **Q318 · 匿名前端信息泄漏？** — 前端可展示任何非秘密运行信息；绝不展示Worker Key、签名私钥或模型凭证。
- **Q319 · privileged容器破坏宿主？** — 将宿主破坏能力列为可信内部MVP的已知风险，不声称安全隔离。
- **Q320 · HIP_VISIBLE_DEVICES被绕过？** — 将其定义为协作式约束；只检测理论/实际漂移，不保证强制隔离。
- **Q321 · 文档权威顺序？** — MUST/状态表/Schema > 失败矩阵 > API协议 > 图示例 > 背景。
- **Q322 · Spec交付文件？** — 单一完整中文文件`docs/agent-task-scheduler-spec.md`，关键规范不拆散；QA另文保存。

## 最后冲突消解（Q323–Q326）

- **Q323 · 最终提交认证模型？** — 选择A：无认证。Master不校验Token，只检查请求声明username是否在本地白名单；Token字段从提交API删除。
- **Q324 · Task/Plan完整性是否保留？** — 选择A：保留SHA-256和Ed25519签名校验，不实现篡改后的自动修复。
- **Q325 · 是否保留最低cleanup失败分支？** — 保留：stop失败→kill失败→CLEANUP_FAILED→不释放GPU/container Lease→Admin CLI人工处理。
- **Q326 · 最终部署假设？** — NFS始终可用；预建容器获批后不被人工删除或改变；Task/Plan不被人工编辑；违反假设后的自动恢复不属于MVP，廉价前置校验仍可保留。

## 最终决定摘要

访谈最终确定的本质不是“通用安全GPU云”，而是一个面向可信内部团队、由Agent完成协商和审核、由确定性程序编译和执行的两节点GPU任务治理系统。它刻意以简单NFS状态、单Master、Docker CLI、K100_AI固定资源和无自动重试换取MVP可实现性；同时通过不可变revision、严格Task/Plan、签名、状态机、gang scheduling、日志与清理协议保留完整可追溯性。

## 审查后技术勘误（不新增产品决策）

两位独立审查者对正式spec做了逻辑审计。以下内容是为了让既有Q1–Q326决定能够自洽实现而补充的协议细化，不代表用户又回答了新的问题，也不改变上面的历史编号与产品边界：

- 调度顺序明确为`PREPARE → PREPARED barrier → COMMIT_LEASE → SIGN_PLAN → PLAN_ACK barrier → START_SETUP → setup barrier → START_RUN`，消除“Plan依赖Lease、Lease又依赖预检/ACK”的循环。
- 一个Execution仍只执行一次业务，但在任何setup开始前可以有多个不可变`DispatchAttempt`；`dispatch_generation`递增。只有所有Worker证明旧代从未开始setup并释放本地锁后，才可安全回队。setup开始后不允许换代或自动重试。
- 每个DispatchAttempt的PrepareManifest、Execution Plan和事件保存在独立NFS目录，不能用新generation覆盖旧Plan。
- GPU、容器和端口Lease增加单调`lease_epoch`。签名有效但epoch过期的延迟Plan同样必须拒绝，防止旧消息复活已撤销分配。
- 大型AGED Task的reservation target是覆盖全部Unit的Worker集合；双机gang必须同时保护两台目标Worker，不能只在单节点阻止小任务插队。
- Task增加`PREPARING`和`FINALIZING`阶段。`FINALIZING`保存`underlying_outcome`，清理成功后进入`COMPLETED/FAILED/TIMED_OUT/CANCELLED`之一，清理无法确认则进入`CLEANUP_FAILED`；所有终态都没有出边。
- 双机某Unit退出0而peer仍运行时进入`RUN_EXITED_WAITING_PEERS`，不提前teardown/stop；全部自然退出后由Master统一发起自然清理。Task总超时使用持久UTC deadline作为跨节点参考，并在Worker侧转换为不会因重连而重置的本地单调deadline。
- run必须保持为前台、attached的`docker exec`，不得用`&`、`nohup`或daemonization提前返回。取消、超时或peer失败直接stop/kill并跳过teardown；自然结束才执行best-effort teardown。
- Docker清理是否成功以`docker inspect`后置条件为准，而不是只看CLI退出码：复用容器必须确认stopped，新建容器必须确认不存在。无法确认时保留整个相关Lease集合。
- Worker空闲时可不保留业务状态，但运行中必然持有supervisor、锁、epoch高水位和未ACK事件，因此不是任意时刻100%无状态。Worker收到Master持久化终态与Harness记录的ACK前保持`REPORTING_TERMINAL`，不得Graceful退出或重新声明idle。
- Worker Controller在supervisor接管前最终失败使用Task原因`WORKER_AGENT_ERROR`并走全gang撤销/清理；接管后的Controller退出不改变Task。Master drain会终止60秒内未完成的Processor/Reviewer，记录`PROCESSING_ERROR`和原`resume_state`后再写clean marker。
- Master Graceful shutdown持久化clean epoch和活动assignment集合；重启后用`assignment_id + dispatch_generation + lease_epoch`与Worker replay对齐。非clean或不匹配状态进入`RECONCILIATION_REQUIRED`，不自动释放或重派。
- NFS通过export、ACL、服务身份和root-squash区分Master元数据区与Worker assignment日志/临时区。为保持Q33/Q162的挂载决定，不新增通用路径黑名单；即使Task挂载`/public/share`父目录，Worker/root-squashed容器也必须无法写Master元数据，并在Master READY前做权限自检。
- Worker产生的Harness调用记录由Worker发送内容/hash或临时URI，Master校验后写入永久`harness-calls/`；Worker不能直接写永久Ground Truth。
- `X-Username`只要求用于提交者与对象变更接口；匿名`/api/v1/observe/*` GET明确豁免。官方前端只发GET是客户端契约，不被描述成服务端真实授权边界。
- `PROCESSING_ERROR`由提交者通过幂等resume接口恢复原阶段；`COMPILE_FAILED`由Admin CLI复用冻结Compilation Context重跑，避免状态可恢复却没有操作入口。
- 删除可绕过不变量的单卡`release-gpu`操作，改为原子`reconcile-resource-set`：只有容器、assignment、generation、epoch、端口和重复GPU观测证据全部一致时才释放整个资源集合。已为`CLEANUP_FAILED`的Task保持终态，仅追加资源已人工处置的审计事件。
