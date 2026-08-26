# 配置系统升级设计

> **当前命名说明（2026-08-25）**：本文只权威定义 Pickel 的配置来源、合并和写回；Runtime 实体与方法名称以 [`Agent Runtime 重构命名约束`](./2026-08-10-agent-runtime-naming.md) 为准。

**状态**：配置合同；**产品名确认 pickel**
**范围**：配置分层、全局会话库与目录过滤、实体命名、与现有模块衔接  
**标杆**：pi（settings / models / auth 拆分）、Claude Code / Codex（全局会话 + 按目录过滤）

---

## 1. 目标

| 目标 | 说明 |
|------|------|
| 拆职责 | 偏好 / 模型 / 密钥 / Agent 定义分文件 |
| 可写 | 运行时可改配置，可选落盘 |
| 可合并 | `默认 < 全局 < 项目 < Agent < CLI(argv) < Environ` |
| 会话全局存、目录筛 | 库在用户家目录；交互列表默认只看当前目录相关会话 |
| 命名直白 | 实体名 = 是什么；不为旧名妥协 |

**非目标**

- 项目 trust 交互
- 远程配置中心
- 为旧类型名保留永久别名层

**产品命名（已确认）**

| 项 | 名称 |
|----|------|
| 产品 / CLI | **pickel** |
| 用户家目录 | **`~/.pickel/`** |
| 项目覆盖目录 | **`.pickel/`** |
| PyPI（现） | `pickel-agent`（可维持或后收） |
| 默认 Agent 人设 | `Pickle`（角色名，可与产品拼写不同） |
| 代码包 | **`pickel`**（保持） |

家目录与项目点目录固定使用 `~/.pickel/` 和 `.pickel/`；不再并行维护其他拼写或第二套路径。

**命名原则（强制）**

- 直接、短、与文件同词：`Settings` ↔ `settings.json`
- **能对齐操作系统概念的，优先对齐**（见 §4.0）
- 配置侧禁止为迁就旧代码引入 Manager / Registry / Runtime / Effective / Store 等空壳层名；Runtime 领域中职责明确的 AgentRegistry、RuntimeHost 等不受此条影响
- 旧实体名与行为冲突时：**改行为、改名或删除**，不叠兼容壳
- 对话 **Session** 只表示会话落库，不借用为「运行时配置」前缀

---

## 2. 现状问题

```text
仓库/
├── config.yaml                 # 唯一配置，只读
├── agents/Pickle/AGENT.md
├── pickle_workspace/           # agent 文件工作区
└── .pickel/sessions.db # 现状：会话库绑在 config 旁（目标改为 ~/.pickel/sessions.db）
```

| 痛点 | 影响 |
|------|------|
| 单文件全能 | 偏好、模型、密钥、agent 混装 |
| 只读加载 | 无法运行时改 model / 默认并落盘 |
| 会话库项目级 | 换目录丢列表；与 Claude/Codex 习惯相反 |
| 命名偏重 | 若引入 Catalog/Manager 会继续欠债 |

---

## 3. 目标布局

### 3.1 目录

```text
~/.pickel/                         # 用户全局
├── settings.json                      # 偏好（可写）
├── models.json                        # 模型目录（无密钥）
├── auth.json                          # 密钥
└── sessions.db                        # 全局唯一会话库

<project>/                             # 项目（cwd 向上发现）
├── .pickel/
│   ├── settings.json                  # 项目覆盖（可选）
│   └── models.json                    # 项目模型覆盖（可选）
└── agents/
    └── Pickle/
        ├── AGENT.md                   # 行为
        └── agent.yaml                 # agent 专属配置
```

**不在此放的**

| 项 | 位置 | 说明 |
|----|------|------|
| `sessions.db` | **仅** `~/.pickel/sessions.db` | 不跟项目、不跟 agent workspace |
| `auth.json` | 默认仅全局 | 不进 git |
| `workspace_path` | `agent.yaml` | 只约束文件工具工作区，与会话库无关 |

### 3.2 分层合并

```mermaid
flowchart TB
  subgraph files["磁盘"]
    D0["内置默认"]
    G["~/.pickel/<br/>settings / models / auth"]
    P["project/.pickel/<br/>settings / models"]
    A["agents/&lt;id&gt;/<br/>agent.yaml + AGENT.md"]
  end

  subgraph mem["进程"]
    CLI["CLI / argv"]
    SP["Environ"]
  end

  D0 --> C["Config 加载合并"]
  G --> C
  P --> C
  A --> C
  CLI --> C
  SP --> C
  C --> AC["AppConfig 只读结果"]
  AC --> Def["解析 AgentDefinition"]
  Def --> Pkg["冻结 AgentPackageVersion"]
```

| 优先级（低→高） | 来源 | 写盘 |
|-----------------|------|------|
| 1 | 内置默认 | 否 |
| 2 | 全局 settings / models / auth | 是（settings/auth） |
| 3 | 项目 settings / models | 是 |
| 4 | Agent 定义 | 文件编辑 |
| 5 | CLI | 否 |
| 6 | Environ（进程环境） | 默认否；显式「写入默认」才写 Settings |

---

## 4. 实体命名（目标态）

### 4.0 与操作系统概念对齐

配置与运行态按 Unix 习惯拆开，而不是按「对话 Session」拆：

| 操作系统概念 | 本系统 | 说明 |
|--------------|--------|------|
| 配置文件 `~/.config`、`/etc` | **Settings** | 持久偏好，可写回文件 |
| 主机/用户资源清单 | **Models** | 模型目录（类似可用命令/驱动清单） |
| 密钥 `.netrc` / keyring | **Auth** | 凭证，权限收紧 |
| 可执行程序 / 角色定义 | **Agents** | `agents/<id>/` 目录 |
| 进程环境 `environ(7)` | **Environ** | 当前**进程**上的可变覆盖，默认不落盘 |
| 启动参数 `argv` | **CLI** | 一次调用传入 |
| 解析后的生效视图 | **AppConfig** | 合并结果，只读 |
| 加载与合并 | **Config** | 入口（类似读配置并 export 到进程） |
| 工作目录 `cwd` | Session.`cwd` / 进程 cwd | 会话归属目录过滤用 |
| 作业日志 / 历史 | **Session** / **Sessions** | 对话落库；**不是**运行配置 |

```text
磁盘配置文件          进程
Settings/Models/...  →  Config 合并  →  AppConfig
                              ↑
                         CLI (argv)
                              ↑
                         Environ（运行中可改，像改环境变量）
```

**Environ 不是 Session 的一部分。**  
Session 回答「这本对话存了什么」；Environ 回答「这个进程此刻用哪个 model / thinking」。

### 4.1 配置侧

| 名称 | 职责 | 对应物 |
|------|------|--------|
| **Config** | 加载、合并、发现路径；入口 | 代码模块 `config` |
| **AppConfig** | 合并后的只读生效视图 | 内存 |
| **Settings** | 读写 `settings.json`（global / project） | `settings.json` |
| **Models** | 读 `models.json`（切换 model 时热读） | `models.json` |
| **Auth** | 读 `auth.json` | `auth.json` |
| **Agents** | 扫 `agents/<id>/`，加载 agent 定义 | 目录 |
| **Environ** | 当前进程运行覆盖（model / thinking 等，默认不落盘） | 进程内存 |

### 4.2 会话侧（与配置正交）

| 名称 | 职责 |
|------|------|
| **ConversationSession** | 一本对话的身份、Conversation Tree 与活动位置 |
| **ConversationNode** | 对话树中的不可变位置 |
| **ConversationStore** | `sessions.db` 中 Session/Node 的持久化窄接口 |

### 4.3 Agent Package Version

Pickel 设置始终是唯一可编辑源，不增加 `package.yaml` 或第二套 Agent 配置。

```text
AppConfig + AgentConfig + ModelConfig + AGENT.md + Skills + ToolBus
                              ↓ resolve
                       AgentDefinition
                              ↓ freeze
                    AgentPackageVersion
                              ↓ load implementations
                     LoadedAgentPackage
```

- `AgentDefinition` 保存现有设置解析后的来源和选择。
- `AgentPackageVersion` 冻结 behavior、`primary/worker/utility` ModelPolicy、无密钥模型参数、AgentRuntimePolicy、WorkspacePolicy、Skill 全文、ToolVersion 与 ExtensionVersion；ID 为规范内容 digest。
- `LoadedAgentPackage` 持有进程内 ToolSnapshot、SkillManifest 和现阶段运行对象，不能直接持久化。
- `api_key`、token、password、authorization 等秘密不得进入 AgentPackageVersion；只记录所需 SecretRef。
- 相同 Pickel 设置和文件内容必须得到相同 `package_version_id`，创建时间不参与 digest。
- Environ、Settings 或 Agent 文件变化只影响未来接受的 Operation；已有 Operation 继续使用其 package_version_id 和 workspace_binding。
- `ModelVersion.provider` 表示服务身份，`wire_protocol` 表示 HTTP wire；
  `provider_implementation` 按 wire protocol 冻结，不能再假设服务商与协议一一对应。
- Runtime Boot 支持 Anthropic Messages、OpenAI Responses 与 OpenAI-compatible
  Chat Completions 三条明确 wire 映射；OpenAI Responses 固定 `store: false`，不使用
  `previous_response_id` 或服务端 Conversation 作为恢复权威。Gemini 仍只保留
  Provider 直接调用测试，不接入 Boot。

### 4.4 废弃 / 禁止的命名

| 不要用 | 原因 | 用 |
|--------|------|-----|
| ConfigRuntime / EffectiveConfig | 空壳层名 | Config / AppConfig |
| SettingsManager | 重 | Settings |
| ModelCatalog | 术语 | Models |
| AuthStore | 重 | Auth |
| 配置侧 AgentRegistry | 与运行时注册表同名 | `Agents`；运行时 `AgentRegistry` 保留 |
| SessionSettings / SessionPatch / SessionOverride | 误绑对话 Session；属进程运行态 | Environ |

旧代码里的 `AppConfig.load(path)`、`from_config_path` 等：**实现阶段直接改行为与调用点**，不保留双轨类型。

```mermaid
flowchart LR
  subgraph old["旧（可删可改）"]
    O1["AppConfig.load yaml"]
    O2["项目旁 sessions.db"]
  end
  subgraph new["新"]
    N1["Config → AppConfig"]
    N2["Settings / Models / Auth / Agents / Environ"]
    N3["Sessions → ~/.pickel/sessions.db"]
  end
  old -->|替换 不叠壳| new
```

---

## 5. 各文件字段

### 5.1 `settings.json`

```json
{
  "default_agent": "Pickle",
  "default_llm": { "provider": "openai", "model": "gpt-5.6-luna" },
  "default_file_access_mode": "full",
  "default_skills_path": ".agent/skills",
  "react_max_steps": 100,
  "context_cli_turn_window": 5,
  "openviking": {
    "enabled": false,
    "timeout_seconds": 30,
    "commit_after_minutes": 30,
    "commit_after_turns": 8,
    "tool_output_max_chars": 4000,
    "session_recall": { "enabled": false, "max_chars": 6000, "limit": 5 }
  }
}
```

| 字段 | 原 config.yaml |
|------|----------------|
| `default_*` / `react_*` / `context_*` | 同名 |
| `openviking.*` 策略 | 同名（密钥除外） |

**Settings 第一批可写字段**：`default_llm`、`default_agent`、`react_max_steps`

### 5.2 `models.json`

无密钥；结构沿用现有 `providers` → models 能力字段。  
项目文件按 provider/model **覆盖合并**全局。打开模型列表时重读。

CPA 的 `gpt-5.6-luna` 使用 OpenAI Provider：

```json
{
  "providers": {
    "openai": {
      "models": {
        "gpt-5.6-luna": {
          "max_output_tokens": 65536,
          "provider_options": {
            "reasoning_effort": "low",
            "reasoning_summary": "auto",
            "parallel_tool_calls": true
          }
        }
      }
    }
  }
}
```

`provider_options.reasoning_effort` 映射到 Responses 的
`reasoning.effort`；`reasoning_summary` 映射到 `reasoning.summary`，只有
Provider 实际返回 summary 时才形成 `ThinkingDelta`，CLI 不展示隐藏思维链。
CPA 仍只走 Responses；任何服务都不做协议自动降级，也不使用
`previous_response_id`。

OpenCode Go 作为一个服务身份同时承载三种 wire，模型必须显式声明协议：

```json
{
  "providers": {
    "opencode-go": {
      "models": {
        "gpt-5.6-luna": { "wire_protocol": "openai-responses" },
        "kimi-k3": { "wire_protocol": "openai-chat-completions" },
        "minimax-m3": { "wire_protocol": "anthropic-messages" }
      }
    }
  }
}
```

`provider=opencode-go` 进入观测与模型身份；请求映射只由冻结的
`wire_protocol` 决定。禁止按模型名前缀猜协议、失败后轮询其他协议，或把同一
服务拆成三个虚假 Provider。

### 5.3 `auth.json`

```json
{
  "providers": {
    "anthropic": {
      "api_key": "${ANTHROPIC_API_KEY_PICKLE}",
      "api_base": "https://api.anthropic.com"
    },
    "openai": {
      "api_key": "${CPA_API_KEY}",
      "api_base": "${CPA_BASE_URL}"
    },
    "opencode-go": {
      "api_key": "${OPENCODE_GO_API_KEY}",
      "api_base": "https://opencode.ai/zen/go/v1"
    }
  },
  "openviking": {
    "base_url": "${OPENVIKING_BASE_URL}",
    "account_id": "${OPENVIKING_ACCOUNT_ID}",
    "user_id": "${OPENVIKING_USER_ID}",
    "user_key": "${OPENVIKING_USER_KEY}"
  }
}
```

权限 `0600`；`${ENV}` 展开；不进 git。

`CPA_BASE_URL` 必须指向 OpenAI-compatible API base（通常以 `/v1` 结尾）；
Provider 在其下只调用 `/responses`。密钥仍以逻辑
`providers.openai.api_key` SecretRef 绑定 Package，不进入
AgentPackageVersion。

### 5.4 `agents/<id>/agent.yaml`

```yaml
workspace_path: pickle_workspace   # 文件工具工作区，≠ 会话库
tools: [list_directory, read_file, ...]
file_access_mode: full
models:
  primary:
    provider: opencode-go
    model: kimi-k3
  worker:
    provider: opencode-go
    model: deepseek-v4-flash
  utility:
    provider: opencode-go
    model: mimo-v2.5
remote_agent_id: ${OPENVIKING_AGENT_ID}
skills_path: null
```

行为文案固定读同目录 `AGENT.md`。  
发现：项目下 `agents/*/` 含 `AGENT.md` 或 `agent.yaml` 即注册。

### 5.5 Environ（进程运行态，内存）

```text
Environ                 # 对齐 Unix process environment：属进程，不属于 Session 表
├── llm: { provider, model } | null  # 只覆盖未来 Operation 的 primary
└── provider_options: { thinking: ... } 等
```

默认不写盘。用户「写入默认」→ 拷进 `Settings`（global/project 文件）。  
与 **Session**（对话落库）正交：换 model 不改 sessions.db 行结构。

---

## 6. 全局会话库与目录过滤

### 6.1 存哪里

```text
~/.pickel/sessions.db     # 唯一库
```

- **全局存储**：所有目录、所有 agent 的会话进同一库  
- **不是** 项目旁 `.pickel/sessions.db`；项目 `.pickel/` 只保存配置覆盖
- **不是** agent `workspace_path` 下的库  

### 6.2 按目录过滤（对齐 Claude Code / Codex）

```mermaid
flowchart TD
  Start["交互启动 / pickel chat"] --> Cwd["记录 cwd 规范化路径"]
  Cwd --> New["新建 Session 时写入 cwd"]
  Cwd --> List["列表默认: WHERE cwd = 当前目录<br/>或前缀规则见下"]
  List --> UI["只展示本目录会话"]
  All["pickel sessions --all"] --> Full["展示库内全部会话"]
  Resume["resume session_id"] --> ById["按 id 取；不限目录"]
```

| 场景 | 行为 |
|------|------|
| 某目录下 `pickel` / `pickel chat` 列会话、选续聊 | **只列出与该目录相关的 session** |
| `pickel sessions`（管理命令） | 默认本目录；**`--all` 列出全部** |
| `pickel sessions delete <id>` | 按 id，不限目录 |
| resume 指定 id | 按 id，不限目录（id 全局唯一） |

### 6.3 配置系统关注的 Session 字段

完整 Session 表结构以数据库实体合同为准；配置与列表过滤只关心：

| 列 | 类型 | 说明 |
|----|------|------|
| `cwd` | TEXT NOT NULL | 创建会话时的工作目录（绝对、规范化） |

- **索引**：`(cwd, updated_at)`，便于按目录列会话  
- **过滤规则（默认）**：`session.cwd == 当前 cwd`（规范化后全等）  
- 若后续需要「子目录可见父目录会话」，再扩展前缀匹配；**第一期全等即可**  
- `workspace_path`（agent 文件沙箱）**不参与**会话归属

```mermaid
flowchart LR
  subgraph global["~/.pickel/sessions.db"]
    S1["session A cwd=/proj-a"]
    S2["session B cwd=/proj-b"]
    S3["session C cwd=/proj-a"]
  end
  CD["在 /proj-a 运行"] --> F["过滤"]
  F --> S1
  F --> S3
  ALL["--all"] --> S1
  ALL --> S2
  ALL --> S3
```

### 6.4 与 agent 工作区的边界

| 概念 | 路径 | 用途 |
|------|------|------|
| 会话库 | `~/.pickel/sessions.db` | 对话历史 |
| 会话归属目录 | Session.`cwd` | 列表过滤 |
| Agent 文件工作区 | `agent.yaml` → `workspace_path` | 读写文件范围 |

三者分离，禁止混用。

---

## 7. 模块衔接

### 7.1 边界

```mermaid
flowchart TB
  subgraph config_pkg["config（包名目标 pickel.config）"]
    Config
    AppConfig
    Settings
    Models
    Auth
    Agents
    Environ
  end

  subgraph app_pkg["app（包名目标 pickel.app）"]
    Host[RuntimeHost]
  end

  subgraph runtime_pkg["运行"]
    Registry[AgentRegistry]
    Agent
    Driver[AgentDriver → OperationDriver]
    Generation[RuntimeGeneration]
  end

  subgraph ui_pkg["交互"]
    Chat["ChatLoop 保留名"]
  end

  subgraph data["持久化"]
    Store[Conversation / Inbox / Operation Stores]
  end

  Config --> AppConfig
  AppConfig --> Host
  Environ --> Host
  Host --> Generation
  Host --> Registry
  Registry --> Agent
  Agent --> Driver
  Driver --> Store
  Chat --> Registry
```

| 现有 | 目标 |
|------|------|
| `AppConfig.load(yaml)` | `Config.load()` → `AppConfig` |
| `providers` 嵌在 AppConfig | `Models` + `Auth` 合成 `ModelConfig` |
| `agents` 大表 | `Agents` 扫目录 |
| `root/.pickel/sessions.db` | `Sessions` → `~/.pickel/sessions.db` |
| `AppAssembly` / `Boot` | `RuntimeHost` Composition Root |
| `AgentRuntimeContext` + `RunDependencies` | 删除；Package 实现进入 LoadedAgentPackage，Host 服务显式注入 RuntimeEffects |
| `AgentCoordinator` / `Run.turn(...)` | `Agent.followup()` → Inbox → AgentDriver |
| `BaseLLMProvider` | **Provider** |
| `DefaultProviderResolver` / `DefaultToolResolver` | 删除；RuntimeGeneration 构建时按 ImplementationRef 解析 |
| `ExecutionStrategy` / `ReActStrategy` | 删除；默认 Tool Loop 由 OperationDriver 推进 |
| `ChatLoop` | **保留名与职责**（CLI 交互壳） |

### 7.2 冻结 ModelVersion 与 Package

```mermaid
sequenceDiagram
  participant Config
  participant Models
  participant Auth
  participant Env as Environ
  participant Package as Package Builder
  participant Loader as RuntimeGeneration Loader

  Config->>Env: 读取未来 Operation 的进程覆盖
  Config->>Models: 能力字段
  Config->>Auth: 非敏感 endpoint + SecretRef
  Config->>Package: ModelSelection + 非敏感参数 + SecretRef
  Package-->>Package: 冻结 AgentPackageVersion
  Loader->>Auth: load 时解析 SecretRef
  Loader->>Loader: 构建 LoadedAgentPackage
```

Auth 的 Secret 值不进入 AgentDefinition 或 AgentPackageVersion；Package 只保存 SecretRef。`api_base` 等非敏感 endpoint 进入 ModelVersion，因此 endpoint 改变会产生新 Package ID，Secret 值轮换不会。接受 Operation 前使用当前 Secret 校验可装载性，恢复时按原 Package Version 再解析 SecretRef。

### 7.3 CLI

| 能力 | 行为 |
|------|------|
| 默认启动 | 发现项目 + 全局配置；会话库固定家目录 |
| `--agent` | 保留 |
| 交互列会话 | 默认当前 `cwd` |
| `pickel sessions` | 默认当前 `cwd`；`--all` 全部 |
| `/model` 或等价 | 改 Environ；可选写入 Settings |
| 旧 `--config yaml` | 仅迁移期：一次性导入后提示改用分层文件；**不长期保留双配置模型** |

### 7.4 运行层（当前合同）

目标调用链：

```text
RuntimeHost(Config + AppConfig + Environ)
├── RuntimeGeneration
│   └── LoadedAgentPackage cache
└── AgentRegistry
    └── Agent
        ├── AgentInbox
        └── AgentDriver
            └── OperationDriver

ChatLoop                         # UI：输入、渲染、斜杠命令
└── AgentHandle.followup/steer/inject/cancel
```

| 类型 | 决议 |
|------|------|
| **ChatLoop** | **保留**名与职责，不改成 Repl |
| **Agent** | Root/Child 平等的消息、取消和等待接口 |
| **AgentDriver** | 消费持久化 Inbox，接受或恢复 Operation |
| **OperationDriver** | 推进默认 Agent Tool Loop，不经 Strategy |
| **RuntimeHost** | 读取 Config，管理 Generation、Registry 和 reload |
| **Provider** | 替代 BaseLLMProvider |
| **Resolver 类** | 只允许按 ImplementationRef 解析的窄函数，不创建通用 Resolver 层 |

```mermaid
flowchart LR
  ChatLoop --> Agent
  Agent --> Inbox[InboxMessage]
  Inbox --> AD[AgentDriver]
  AD --> OD[OperationDriver]
```

不为未来 PlanExecute/Reflection 预留 Strategy 接口。出现真实且不可由 Package/Prompt/Tool 合同表达的第二种流程后，才讨论窄用途 `RunWorkflow`。

---

## 8. 运行时改配置

| 操作 | 对象 | 持久化 |
|------|------|--------|
| 进程内换 model | Environ | 否 |
| 设为默认 model | Settings | 是 |
| 改 thinking | Environ（或 models 默认） | 可选 |
| 改 tools | `agent.yaml` | 文件 |
| 改密钥 | Auth 文件 | 是 |

**Settings 写回**

1. deep merge；数组整表替换  
2. 按 scope（global / project）只写变更字段  
3. 文件锁  
4. API 分离：`set_environ(...)` 只改进程覆盖；`set_settings(..., save=True)` 写入文件

Environ 变化不修改 ConversationSession 或 active Operation。下一个 Operation 接受时，Package Builder 使用最新 Environ 冻结新的 AgentPackageVersion；waiting/resume Operation 继续使用原 package_version_id。Agent `models.primary/worker/utility` 都会冻结并装载；角色缺失保持 `None`，不把 primary 隐式复制到其他角色。

Agent 经工具改配置：第一期可不做；先做 CLI / 斜杠命令。密钥字段禁止 agent 随意写。

---

## 9. 迁移

### 9.1 字段

| 原 config.yaml | 新位置 |
|----------------|--------|
| 默认项 / openviking 策略 | `settings.json` |
| providers 能力 | `models.json` |
| api_key / api_base / openviking 密钥 | `auth.json` |
| `agents.<id>.*` | `agents/<id>/agent.yaml`；旧 `llm` 一次性迁为 `models.primary` |
| behavior 路径 | `agents/<id>/AGENT.md` |

### 9.2 会话库

| 原 | 新 |
|----|-----|
| `<project>/.pickel/sessions.db` | `~/.pickel/sessions.db` |

迁移时：

1. 若全局库不存在，可将项目旁旧库 **移动或导入** 到全局路径  
2. 为每条 session **补 `cwd`**：无则填迁移时的 project_root（或 unknown 标记，仅 `--all` 可见）  
3. 项目旁旧 `sessions.db` 备份后删除，避免双库  

### 9.3 命令示意

```text
pickel config migrate --from config.yaml
  → 写 ~/.pickel/{settings,models,auth}.json
  → 写 agents/*/agent.yaml
  → 迁 sessions.db 到全局并补 cwd
  → 备份 config.yaml
```

---

## 10. 路径发现

```mermaid
flowchart TD
  S["启动"] --> H["读 ~/.pickel/*"]
  S --> U["从 cwd 向上找项目<br/>.pickel/ 或 agents/"]
  U -->|有| P["读项目 settings/models<br/>扫 agents/"]
  U -->|无| G["仅全局 + 内置"]
  H --> M["Config 合并"]
  P --> M
  G --> M
  M --> AC["AppConfig"]
  S --> DB["Sessions 固定<br/>~/.pickel/sessions.db"]
```

| 路径 | 基准 |
|------|------|
| settings 相对路径 | project_root，否则 cwd |
| agent.workspace_path | project_root |
| sessions.db | **始终** `~/.pickel/sessions.db` |
| Session.cwd | 创建时的进程 cwd（绝对规范化） |

---

## 11. 分阶段

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0** | Config / Settings / Models / Auth；路径用 `~/.pickel`；会话库全局 + `cwd`；RuntimeHost 读取 AppConfig | chat 可用；列表默认可按目录滤 |
| **P1** | Agents 目录发现；迁移命令；删对 yaml 大表依赖 | 无 `config.yaml` agents 段可运行 |
| **P2** | Settings 写回；Environ；`/model` 等；Package Builder 冻结未来 Operation 设置 | 运行时改配置且不改变 active Operation |
| **P3** | 去掉旧 `--config` 和项目旁 `.pickel/sessions.db` 读取路径 | 与本文一致 |
| **后话** | 只有真实第二种流程出现时讨论 RunWorkflow | 不预留 Strategy 公共层 |

---

## 12. 设计约束

1. 一份职责一份文件：settings / models / auth / agent  
2. 密钥不进 models、不进 git  
3. AppConfig 只读；写只经 Settings / Auth 文件 API  
4. Environ 属进程、默认不持久；与 Session 落库分离  
5. **sessions.db 全局唯一**；列表默认按 `cwd` 过滤；`--all` 看全部  
6. **命名不欠债**：旧名直接改删，不叠兼容类型  
7. agent workspace ≠ session cwd ≠ 会话库路径  
8. 产品与路径统一 **pickel**（`pickel` CLI、`pickel` 包、`~/.pickel`、`.pickel`）

---

## 13. 标杆对照

| 能力 | Claude / Codex | pi | 本设计 |
|------|----------------|-----|--------|
| 会话存储 | 用户级 | `~/.pi/.../sessions` | `~/.pickel/sessions.db` |
| 列表过滤 | 当前目录相关 | 会话目录可配 | 默认 `cwd` 全等；`--all` 全库 |
| settings 分层 | user / project | global / project | 同左 |
| 多 Agent 目录 | 弱 | 弱 | `agents/<id>/` 强化 |
| 实体命名 | 直白 | 略框架 | **短名：Config / Settings / …** |

---

## 14. 小结

- 配置：分层文件 + **Config** 合并 → **AppConfig**；写用 **Settings**，模型用 **Models**，密钥用 **Auth**，角色用 **Agents**，进程覆盖用 **Environ**。  
- 产品：**pickel**（CLI / Python 包 / `~/.pickel` / `.pickel`）。
- 会话：**全局一个 db**（`~/.pickel/sessions.db`）；交互只看当前目录；管理命令可看全部。  
- 运行：**RuntimeHost** → **AgentRegistry** → **Agent / AgentDriver** → **OperationDriver**；ChatLoop 只作为 UI，Provider 去 Base 前缀。
- 配置命名与路径以本文为准；Runtime 命名以命名合同为准。实现时改旧代码，不为旧账保留第二套实体。
