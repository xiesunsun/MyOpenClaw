# 工具热插拔 / 版本管理 / Sandbox / Agent Shell —— 调研

调研目的：为「工具（内置 + MCP + extension）全热插拔 + 工具与 skill 版本管理 + sandbox + 好用的 shell」这条升级线提供事实基础与参考实现对照。本篇只记录调研结论，不含设计定稿。

日期：2026-07-26 · 分支：`feat/tools-and-sandbox`

---

## 一、项目现状盘点

### 1.1 工具层

| 项 | 现状 | 位置 |
| --- | --- | --- |
| 注册表 | `dict[name, BaseTool]`，构造时一次性注入；只有 `register` / `resolve` / `resolve_many` | `tools/registry.py` |
| 缺失能力 | 无 `unregister`、无 `list`、无来源标识（builtin/mcp/extension）、无版本、无启停开关 | 同上 |
| 工具清单 | `builtin_tools()` 硬编码 11 个工具的 Python 列表 | `tools/catalog.py:16` |
| 装配点 | `Run.open()` 内 `ToolRegistry(tools=builtin_tools())` → `resolve_many(agent.tool_ids)`，**工具集在 Run 生命周期内冻结** | `runs/run.py:80-81` |
| 热重载粒度 | 只有 `Run.reload()`：整体重建 Run（连带重建 provider、shell manager、workspace 服务），保留 Environ | `runs/run.py:119-135` |
| 工具选择 | `agent.yaml` 的 `tools: []` 声明 id 列表，静态 | `config/agents.py`（`_YAML_FIELDS`） |
| 执行上下文 | `ToolExecutionContext` 5 字段，`workspace_files` / `shell_session_manager` 是 `Any` —— 弱类型服务注入，新增依赖需改 dataclass | `tools/base.py:22-27` |
| MCP | `pyproject.toml` 已声明 `mcp[cli]>=1.26.0`，**`src/` 零引用** —— 未实现 | — |
| extension | 零痕迹 —— 未实现 | — |

结论：`ToolRegistry` 只是一个静态查找表，不是总线。热插拔需要的注册表可变性、来源分层、生命周期、版本，全部缺失。

### 1.2 Skill 层

| 项 | 现状 |
| --- | --- |
| 发现 | `SkillRegistry.discover(path)` 扫目录下 `SKILL.md` / `skill.md` 的 frontmatter |
| 元数据 | **只认 `name` + `description`**，其余 frontmatter 字段被忽略 |
| 注入 | 组装成 `Available skills:` catalog 进 system prompt（名 + 描述 + 文件路径），由 agent 自行 read |
| 热度 | prepare 管道每 turn re-discover（`boot.py:52-53` 注释确认）→ **skill 已是文件级热插拔** |
| 缺失 | 无版本、无依赖声明、无 allowed-tools、无来源（本地/远程）、无生命周期状态、无 agent 自管理写入通道 |

结论：skill 的「热」已经有了，缺的是「可被 agent 管理 + 可版本化 + 可审批」。

### 1.3 Shell

实现：PTY（`pty.openpty`）+ 长驻 `/bin/bash --noprofile --norc -s`，marker 协议回传 `exit_code` 与 `PWD`（`\x1f` 分隔），按 session_id 复用。`tools/shell.py`。

已发现缺陷：

| 缺陷 | 表现 | 影响 |
| --- | --- | --- |
| ANSI 转义未清洗 | `_normalize_output` 只删 `\r`；bracketed paste `\x1b[?2004h/l` 混入输出 | **当前 `tests/tools/test_shell.py` 6 例失败**（Linux bash 默认开启，macOS 上不触发） |
| `truncated` 是死字段 | 恒为 `False`，无任何截断逻辑 | 大输出直灌上下文 |
| stdout / stderr 合并 | 都接到同一 slave_fd | agent 无法区分错误流 |
| 超时即杀 | 超时后 `interrupt()` + `terminate()` 整个 shell | 丢失 cwd、环境变量、后台进程等全部会话状态 |
| 无后台任务 | 单命令串行 `_running` 锁 | 跑 dev server / 长任务即阻塞 |
| 无 stdin 交互 | 无向运行中进程写入的通道 | 交互式命令（含 sudo 提示）直接卡死 |
| 旧命名残留 | marker 前缀 `__MYOPENCLAW_DONE_` | — |

### 1.4 权限与隔离

| 层 | 现状 |
| --- | --- |
| 文件 | `FileAccessPolicy` 两档：`WorkspacePathAccessPolicy`（限 workspace 内）/ `FullAccessPathPolicy`（放开）。`tools/policy.py` |
| 工具调用 | `hooks/lifecycle.py` 有 `pre_tool_use` 决策点，可 block（`merge_pre_tool_decisions`） |
| Shell | **零限制** —— PTY 直起宿主 bash，继承全部环境变量与凭据，路径策略对它完全无效 |
| 网络 | 无任何限制 |
| 进程 / 资源 | 无 cgroup、无 pids/内存上限 |
| Sandbox | **不存在** |

结论：文件工具受 workspace 约束，但 `shell_exec` 是一个绕过全部策略的后门。当前架构下 agent 自改代码 = 直接改宿主。

---

## 二、设计意图（两篇博客）

**`why-myopenclaw-needs-sandbox`（2026-04-26）**

- sandbox 从「可延后的安全功能」变成「runtime 能力的前提」。
- 自进化设想：agent 在受控条件下修改自己的代码、工具、能力结构 —— 拥有代码仓库、自动开分支、修改 skill、热重载。
- sandbox 被重新定义为**权力关系管理**，而非单纯安全措施。四个核心问题：能力范围界定（文件/凭据/网络）、变更审查机制（哪些自动、哪些需审）、可回滚性（哪些环境可销毁、哪些状态可回滚）、跨 agent 隔离。
- 结论原句：「当 Agent 只调工具时，sandbox 像可后补的问题；但想让它进化、自修改时，sandbox 就成了能力成立的前提。」

**`evolutionary-algorithm-thoughts`（2026-04-18）**

- LLM 把「变异」从盲目参数抖动升级为**带语义偏差的方案改写**，搜索从噪声盲搜变成带先验的生成。
- 适用判据：「很难被精确定义，但又很容易被评价」的任务 —— 提示词结构、工作流设计、行为策略。
- **自动评估器是闭环的必要条件**：测试成功率、工具调用结果、成本统计、对抗式评价。没有环境裁决，搜索会「迅速退化成自我陶醉」。
- 版本管理是进化框架的天然需求：多版本保留 + 演进历史追踪 → 支持回滚与热插拔比对。

两篇合起来给出的约束：**热插拔 + 版本管理 + 沙箱 + 自动评估器，是自进化的四个前提，缺一不可。** 沙箱不是安全附件，是自修改的许可证；版本管理不是运维便利，是进化的种群机制。

---

## 三、参考实现对照

### 3.1 工具注册与热插拔

| | Pi | Claude Code | Hermes |
| --- | --- | --- | --- |
| 扩展形态 | TypeScript extension，`export default function (pi: ExtensionAPI)` 工厂函数 | plugin：自包含目录 + `.claude-plugin/plugin.json` | skill（含脚本）+ MCP |
| 注册 API | `pi.registerTool({name, description, promptSnippet, promptGuidelines, parameters, execute, renderCall, renderResult})` | plugin 贡献 skills / agents / hooks / mcpServers / LSP / monitors | skill 目录 + `skill_manage` 工具 |
| 自动发现 | 全局 `~/.pi/agent/extensions/*.ts` + 项目 `.pi/extensions/*.ts` + settings 配置路径 + CLI `-e` | marketplace 安装到 `~/.claude/plugins/cache`；skills 目录内放 `plugin.json` 可就地发现（免安装） | `~/.hermes/skills/<name>/SKILL.md` |
| **运行时动态注册** | **可以**：在 `session_start` / command / 任意 event handler 里 `registerTool`，立即生效、无需 reload；`pi.setActiveTools(names)` 运行时启停 | 需 `/reload-plugins` | agent 通过 `skill_manage` 创建后即可用 |
| 显式热重载 | `/reload`：`session_shutdown` → 重载 extensions/skills/prompts/themes/context files → `session_start{reason:"reload"}` | `/reload-plugins` 切换 hooks/MCP/LSP 到新版本路径；monitor 需重启会话 | — |
| 工具拦截 | `tool_call` 事件可 `{block:true}` 或原地改参；`tool_result` 可 patch 输出，多 handler 按加载序链式 | `PreToolUse` / `PostToolUse` hook | 命令审批工作流 |
| 信任门 | `project_trust` 事件先于 `.pi/` 加载；`{trusted, remember}` 落 `trust.json` | 项目作用域 plugin 走 `.claude/settings.json` 同一信任门，且运行代码的组件受限 | `guard_agent_created` 扫描危险模式 |

**关键可借鉴点**

1. Pi 的「动态注册立即生效」+「setActiveTools 运行时启停」是热插拔的最小充分形态：注册表可变 + 激活集可变，两者分离。
2. Pi 的 `resources_discover` 事件让 extension 贡献 skillPaths / promptPaths —— 发现路径本身可插拔。
3. Pi 的 `prepareArguments(args)` 是**参数 schema 演进的兼容 shim**：resume 旧 session 时把旧参数迁移到新 schema。这是工具版本管理里最便宜、最实用的一招。
4. Pi 明确禁止在 extension 工厂里启长驻资源（watcher / timer / 进程），必须放 `session_start` 并在 `session_shutdown` 清理 —— 热插拔正确性的关键纪律。
5. 三家都把「项目目录来的扩展」和「用户自己的扩展」分成两个信任等级。

### 3.2 版本管理

| | 机制 |
| --- | --- |
| **Pi** | `settings.json` 里 `packages: ["npm:@foo/bar@1.0.0", "git:github.com/user/repo@v1"]` —— 直接复用 npm / git 的版本语义；多文件扩展用 `package.json` 声明 deps 与 `pi.extensions` 入口 |
| **Claude Code** | `plugin.json` 的 `version`（semver）；**省略则用 git commit SHA 当版本**，每次 commit 视为新版本。`dependencies: [{name, version: "~2.1.0"}]` semver 约束。每个安装版本一个独立缓存目录；更新/卸载后旧目录标记 orphaned，**14 天后清理**（宽限期让已加载旧版本的并发会话继续运行）。`${CLAUDE_PLUGIN_ROOT}` 随版本变、`${CLAUDE_PLUGIN_DATA}`（`~/.claude/plugins/data/{id}/`）跨版本持久。`claude plugin validate --strict` 供 CI |
| **Hermes** | skill 演进走 **PR 机制**，保留完整 git 历史；Darwinian Evolver 采用 "Git-based organisms" 做代码级进化 |

**关键可借鉴点**

1. **代码目录按版本隔离 + 数据目录跨版本持久** 是必须的二分，否则升级即丢状态。
2. **旧版本延迟回收（宽限期）** 解决「运行中的会话仍持有旧版本」这个热插拔必然遇到的问题。
3. 版本号缺省回落到 git SHA —— 对「agent 自己改自己」的场景恰好合适：不要求 agent 每次自觉 bump semver，git 历史即版本序列。
4. 依赖用 semver 约束 + CI 校验（`--strict`）。

### 3.3 Skill 管理与自进化闭环（Hermes 最完整）

| 环节 | 机制 |
| --- | --- |
| agent 自管理 | `skill_manage` 工具，动作 `create` / `patch` / `edit` / `delete`；**`patch` 是文档推荐的定点修复方式** |
| 写入审批 | `skills.write_approval: true` → 所有 agent 写入暂存到 `~/.hermes/pending/skills/`，经 `/skills pending`、`/skills diff <id>`、`/skills approve|reject <id>` 处置；可运行时 `/skills approval on\|off` |
| 内容护栏 | `skills.guard_agent_created: true` → 写入前扫描危险模式（凭据收集、prompt injection、外泄） |
| 生命周期 | 自治 Curator 每 7 天跑（需至少空闲 2h），skill 状态迁移 `active` → `stale`(30 天未用) → `archived`(90 天) |
| skill 声明依赖 | frontmatter `required_environment_variables: [...]`，Hermes 从 shell env → `~/.hermes/.env` 解析，按当前 backend 转发（`docker_forward_env` / SSH passthrough）；skill 的 config 需求也在 frontmatter 声明，加载时注入 skill 上下文 |
| 进化优化 | 独立项目 `hermes-agent-self-evolution`：DSPy + GEPA（Genetic-Pareto）反射式 prompt 进化 —— **读执行 trace 理解「为什么失败」而非只知道「失败了」**，提出定向 mutation |
| trace 来源 | 合成评估数据（`--eval-source synthetic`）或真实会话历史（`sessiondb`，含 Claude Code / Copilot / Hermes 的 session） |
| 候选门控（5 层） | ① `pytest tests/ -q` 100% 通过 ② 尺寸限制：skill ≤15KB、工具描述 ≤500 字符 ③ **缓存兼容性：禁止对话中途变更** ④ 语义保持（防偏离原目标）⑤ 人工 PR 审查，禁止直接提交 |
| 成本 | 每次优化 ~$2–10，纯 API 调用，无需 GPU 训练 |
| 交付状态 | ✅ skill 文件优化；🔲 工具描述、system prompt、代码实现、持续改进管道 |

**关键可借鉴点**

1. `patch` > 全量重写 —— agent 自改的默认动作应该是最小 diff。
2. **暂存 + diff + 审批** 是「agent 可写自己」与「人保留控制权」的唯一可行交汇点。博客里的「哪些修改自动执行、哪些需审查」在这里有了具体形态。
3. **缓存兼容性门控**（禁止对话中途变更）—— 这条最容易被忽略：热插拔一旦在 turn 中间改了工具定义或 system prompt，prompt cache 全部失效、上下文自相矛盾。**热插拔必须落在 turn 边界。**
4. 5 层门控就是博客所说的「自动评估器」的具体清单：可执行的测试 + 可度量的尺寸 + 结构约束 + 语义约束 + 人审。
5. Curator 的 active/stale/archived 迁移解决了「进化只增不减」导致的 skill 目录膨胀与 catalog 上下文膨胀。

### 3.4 Sandbox

**Claude Code（进程级 OS 沙箱，两层独立）**

| 层 | 机制 |
| --- | --- |
| 文件系统 | macOS Seatbelt / Linux+WSL2 bubblewrap（需 `bubblewrap` + `socat`，可选 seccomp filter 用于阻断 unix domain socket）。默认：**写** = cwd + session `$TMPDIR`；**读** = 全机除 `denyRead`。配置 `allowWrite` / `denyWrite` / `denyRead` / `allowRead`，路径更具体者胜；多 scope 数组合并（deny 只能收窄，任何 scope 可加、无 scope 能移除） |
| 网络 | sandbox 外的 proxy 强制域名 allowlist。**默认零预允许**，首次访问新域名提示；`allowedDomains` / `deniedDomains`；managed `allowManagedDomainsOnly` 锁定 |
| 凭据 | `credentials.files: [{path, mode:"deny"}]`（读拒绝）+ `credentials.envVars: [{name, mode:"deny"|"mask"}]`。**`mask` 极妙**：沙箱内只见 per-session sentinel，请求出站到 `injectHosts` 时由 proxy 替换成真值 → 工具能认证，但命令与日志永不持有真凭据（需 `network.tlsTerminate`） |
| 自我提权防护 | **沙箱自动拒写各 scope 的 `settings.json` 与 managed settings 目录**，符号链接也解析后加入 deny |
| 逃生口 | `excludedCommands`（如 `docker *`）；工具参数 `dangerouslyDisableSandbox`（失败后模型可重试，走常规审批）；`allowUnsandboxedCommands: false` = 严格模式，参数被完全忽略；`failIfUnavailable: true` = 依赖缺失则拒绝启动 |
| 与权限的分工 | 权限规则在**命令跑之前**按命令串判定，覆盖所有工具；沙箱由 **OS 在运行中的进程上**强制，只覆盖 Bash 及其子进程 —— 「即使允许的命令做了名字之外的事，边界仍然成立」 |
| 已知限制 | 默认不终止 TLS → 可能 domain fronting 外泄；`allowUnixSockets` 放 `docker.sock` 等于给出宿主；容器内需 `enableWeakerNestedSandbox`（显著削弱）；写权限过宽（`$PATH` 目录、`.bashrc`）可提权 |
| 复用 | 同一套原语已单独发包 `@anthropic-ai/sandbox-runtime` |

**Hermes（后端级隔离，6 个可选 backend）**

| Backend | 隔离 | 延迟 | 状态持久 | 场景 |
| --- | --- | --- | --- | --- |
| local | 无（靠上层命令审批，如 `rm -rf` 提示） | <10ms | 磁盘 | 一次性 VM、单人开发 |
| **docker** | 容器 | ~50ms | 按容器 | **官方建议 ~80% 用户选此** |
| ssh | 取决于远端 | 远程 | 是 | 基础设施操作 |
| singularity | HPC 容器 | ~100ms | 按容器 | HPC / 科研 |
| modal | serverless 容器 | ~200ms 冷启 | 可快照 | 突发算力、GPU |
| daytona | serverless workspace | ~300ms 冷启 | **休眠可保留** | 长期 agent 环境、近零空闲成本 |

Docker backend 细节（直接可抄的形态）：单一长驻容器跨 session 共享，用 label（`hermes-agent=1` / `hermes-task-id` / `hermes-profile`）探测并重连、复用文件系统状态；后台进程默认跨 session 存活；仅在 `docker_persist_across_processes: false` / 空闲超时 / 显式操作 / orphan reaper 时销毁。安全加固：`--cap-drop ALL` 仅选择性保留 `DAC_OVERRIDE` `CHOWN` `FOWNER`、`--security-opt no-new-privileges`、`--pids-limit 256`、tmpfs 限额（`/tmp` 512MB、`/var/tmp` 256MB、`/run` 64MB）。资源上限 `container_cpu` / `container_memory` / `container_disk`。`docker_network: true|false`（false = `--network=none` 气隙）。卷挂载 `docker_volumes`（支持 `:ro`），**skills 目录与 skill 声明的凭据文件自动以只读卷挂进容器**。

Hermes 的 shell 相关配置：`persistent_shell: true`（长驻 bash）、`timeout`（每命令，默认 180s）、`lifetime_seconds: 300`（空闲回收）、`home_mode: auto|real|profile`（子进程 HOME 策略）、`env_passthrough`。输出治理：`tool_output.max_bytes: 50000` / `max_lines: 2000` / `max_line_length: 2000`；`file_read_max_chars: 100000`（超限拒绝并要求 offset/limit 分页）。

**两条路线的关系**：Claude Code 是「同机进程级细粒度边界」，Hermes 是「换执行后端整体隔离」。二者不冲突且互补 —— 后端选型决定隔离强度上限，进程级策略决定同一后端内的细粒度。Hermes 的 local backend 恰好对应「没有沙箱时靠审批兜」，与本项目现状一致。

### 3.5 Agent Shell 的既有解法

| 需求 | 参考做法 |
| --- | --- |
| 输出上限 | Hermes `tool_output.max_bytes / max_lines / max_line_length` 三档；读文件超限直接拒绝并要求分页（而非静默截断） |
| 长驻会话 | Hermes `persistent_shell` + `lifetime_seconds` 空闲回收；Docker backend 让后台进程跨 session 存活 |
| 超时 | 每命令 timeout（Hermes 默认 180s）；Pi 用 `AbortSignal` 贯穿工具与事件处理，Esc 可取消进行中的异步工作 |
| 并发与文件竞争 | Pi 工具默认并行执行，文件写入用 `withFileMutationQueue(absolutePath, fn)` 串行化 |
| 环境策略 | Hermes `home_mode`（auto/real/profile）、`env_passthrough`、`docker_forward_env` 与 `docker_env` 分开（转发宿主变量 vs 注入字面量） |
| 危险命令 | Claude Code：`rm`/`rmdir` 指向 `/`、home、关键系统路径时即便在沙箱内、即便 auto-allow 也强制提示 |

---

## 四、可直接落到设计的判断

1. **热插拔必须落在 turn 边界。** 工具集/system prompt 在 turn 中间变更会同时破坏 prompt cache 与上下文一致性（Hermes 把「缓存兼容」列为独立门控）。正确形态：注册表随时可变，**生效点在 turn 开始时快照**。
2. **注册表可变性与激活集要分离。** Pi 的 `registerTool` + `setActiveTools` 是两件事：前者管「存在哪些工具」，后者管「这一 turn 暴露哪些给模型」。当前 `agent.tool_ids` 只有后者的静态版本。
3. **`ToolRegistry` 需要重做成总线**：来源分层（builtin / mcp / extension）+ 版本 + 启停 + `unregister` + 列举。`Run.open` 里的一次性构造要换成注入一个长生命周期的 registry。
4. **`ToolExecutionContext` 的 `Any` 字段是热插拔的直接障碍** —— 第三方工具需要什么服务无法声明。需要一个显式的能力/服务容器。
5. **版本管理用 git 而非自造。** 版本号缺省回落 commit SHA（Claude Code 的做法）对「agent 自改」最合适；代码目录按版本隔离、数据目录跨版本持久；旧版本延迟回收给运行中会话留宽限期。
6. **skill 需要三样东西才能被 agent 管理**：`skill_manage` 式的定点 `patch` 工具、暂存+diff+审批通道、内容护栏扫描。skill 发现已经是热的，不用重做。
7. **Sandbox 分两层实现，可分期**：先做「进程级边界」（Linux bubblewrap + 网络 proxy allowlist + 凭据 deny/mask + 拒写自身配置），后做「后端可换」（local / docker / ssh / 云 sandbox）。前者收益立刻兑现在 `shell_exec` 这个后门上。
8. **`shell_exec` 是当前唯一绕过全部策略的通道** —— 沙箱先落在它身上，而不是先动文件工具（文件工具已有 workspace 策略）。
9. **沙箱必须拒写自身配置与自身代码目录**，否则「agent 自改」等于「agent 自我提权」。这是自进化场景下最容易被忽略的一条。
10. **自动评估器是自进化的准入条件，不是后续优化。** 没有「测试通过 + 尺寸限制 + 语义保持 + 人审」这套门控，热插拔 + 版本管理只是让 agent 更快地把自己改坏。GEPA 那种「读 trace 理解失败原因」的能力依赖 trace 可得 —— 本项目刚做完的 runtime 事件与可观测性正是这个前提。

---

## 五、范围分解建议

用户描述的整体是多个独立子系统，不宜一份设计稿覆盖。建议拆成五个子项目，各自走「设计 → 计划 → 实施」：

| # | 子项目 | 内容 | 依赖 | 独立价值 |
| --- | --- | --- | --- | --- |
| **T1** | 工具总线与热插拔 | `ToolRegistry` 重做为可变总线（来源分层 / 启停 / unregister / 列举）；激活集与注册表分离；turn 边界快照；`ToolExecutionContext` 换成显式服务容器；`Run.open` 改为注入长生命周期 registry | 无 | 后续三项的地基 |
| **T2** | 工具来源接入 | MCP 客户端（`mcp[cli]` 依赖已在）+ extension 装载机制（自动发现目录 + 信任门 + 生命周期钩子 + 动态注册） | T1 | 打通「内置 + MCP + extension」三来源 |
| **S1** | Shell 升级 | 修 ANSI 清洗（当前 6 个失败测试）、输出三档上限、stdout/stderr 分离、超时不杀会话、后台任务、stdin 交互、危险命令拦截 | 无 | **立刻可交付，修现存失败测试** |
| **S2** | Sandbox | 进程级：Linux bubblewrap + 网络 proxy allowlist + 凭据 deny/mask + 拒写自身配置/代码 + 逃生口与严格模式；后端级：backend 抽象（local / docker / ssh），落在 `shell_exec` | S1 | 自修改的许可证 |
| **V1** | 工具与 skill 版本管理 | git 为底的版本模型；skill frontmatter 扩展（版本 / 依赖 / 所需环境变量 / 生命周期状态）；`skill_manage` 定点 patch；暂存+diff+审批；内容护栏；工具参数 schema 演进 shim | T1、S2 | 进化的种群机制 |

依赖关系：`T1 → T2`，`T1 + S2 → V1`，`S1 → S2`。`S1` 与 `T1` 相互独立，可并行。

自进化闭环（评估器 + trace 驱动优化）是 V1 之后的第六项，本轮不设计。

---

## 参考来源

- [Why MyOpenClaw needs sandbox](https://blog.sunxie.me/essays/2026/04/26/why-myopenclaw-needs-sandbox.html)
- [Evolutionary algorithm thoughts](https://blog.sunxie.me/essays/2026/04/18/evolutionary-algorithm-thoughts.html)
- [Pi Extensions 文档](https://pi.dev/docs/latest/extensions) · [earendil-works/pi](https://github.com/earendil-works/pi)
- [Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing) · [Plugins Reference](https://code.claude.com/docs/en/plugins-reference) · [@anthropic-ai/sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
- [NousResearch/hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution) · [Hermes Configuration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuration.md) · [Hermes 的 7 个 sandbox backend 选型](https://hermesagents.net/blog/seven-sandbox-backends-choose/)
