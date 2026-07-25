<p align="center">
  <img src="./.github/assets/pickel_agent_logo.png" alt="Pickel Agent Logo" width="360"/>
</p>

<h1 align="center">Pickel Agent</h1>

<p align="center">
  <strong>本地优先、高度可扩展的 AI 编码与工作空间自动化 Agent 运行期系统 (Runtime)</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <a href="./README.en.md">English</a>
</p>

---

**Pickel Agent** 是一个专为本地编码与工作空间自动化场景设计的**本地优先 (Local-First)、高度可扩展的 AI Agent 运行期系统 (Runtime)**。它专注于解决 AI 开发助手在本地落地时的核心运行问题：**工具的安全与受控执行、多轮持久化会话的管理、高保真度的 PTY 终端持久化、精准的上下文 Token 剪裁控制，以及云端协同同步。**

与许多仅停留在提示词（Prompt-only）层面的演示不同，Pickel Agent 提供了工业级的本地 CLI 引擎和极其灵活的 Skills 插件系统，允许开发者直接使用 Python 快速构建并热插拔各类开发工具，无缝接入主流的大语言模型（如 Gemini、Claude）。

---

## 核心主张与技术特色

### 1. 绝不丢失上下文的 PTY 持久化终端
传统 Agent 执行 Shell 命令往往是单次短暂的 `subprocess`，无法保留工作目录、环境变量及前置命令状态（如在执行 `npm install` 后无法在同状态下继续执行 `npm run dev`）。Pickel Agent 独创 PTY 驱动的持久化终端会话，使 AI Agent 能够像真人一样保持会话环境的连续性。

### 2. 离线沙箱与绝对安全受控的工具箱
工具执行的安全至关重要。Pickel Agent 自带细粒度的工作空间访问控制策略（Full/Sandbox），提供列目录、内容检索、精准代码替换、增量写入等 10+ 种高安全性的内置工具，让 AI 在受控的目录中起效，杜绝破坏系统文件的隐患。

### 3. 基于 /context 与 OpenViking 的智能上下文协同管理
在长对话或大型编码任务中，Token 的开销与历史召回的质量至关重要。Pickel Agent 提供创新的双层上下文管理机制：
- **本地微观观测 (`/context`)**：提供极佳的 Token 滑动窗口编排算法，避免盲目发送历史全文；用户在 CLI 终端中可随时通过 `/context` 快捷指令诊断当前的 Token 消耗大小及精确窗口边界，做到开销完全透明可控。
- **云端宏观同步与关联召回 (`OpenViking`)**：内置可选的 OpenViking 适配层。不仅支持将本地会话实时同步至远端，还支持基于时间和对话轮次（Turn Threshold）的会话状态自动提报提交，并能在新轮次中利用语义检索自动召回（Session Recall）大跨度的关联历史上下文，打破单机内存屏障。

### 4. 强大的多 Agent 隔离与可插拔 Skills 架构
应对复杂、多层次的自动化任务，Pickel Agent 原生支持多 Agent 生态与技能隔离：
- **多 Agent 并存与环境隔离**：在 `config.yaml` 中，您可以为不同职责的 Agent（例如编码专家 `Pickle`、架构师 `Architect` 等）独立指定其专有的工作空间路径（Workspace Path）、系统提示词（System Prompt）、受控工具白名单、以及不同大模型 Provider 驱动。通过 `--agent` 参数即可一键拉起独立沙箱，实现多角色快速切换。
- **热插拔 Skills 技能树**：开发者只需简单编写 Python 逻辑和 Markdown 规则说明，模块化 Skills 即可被特定 Agent 自动发现并动态注入其 Tool 注册表和 System Instructions 中，极易扩展和生态复用。

---

## 核心功能视效演示

### 1. 命令行交互与极速启动 (CLI Chat Loop & Agent Startup)
直接通过命令行拉起流畅的 AI 交互终端，可随时根据配置文件与选择的 Agent 瞬间就绪。
![极速启动](./.github/assets/pickel_agent_start.png)

### 2. 强大的 ReAct 协同思维闭环 (ReAct Reasoning Pattern)
Agent 在后台清晰地展示 思考 (Thought) -> 决策 (Action) -> 观测成果 (Observation) 的完整 ReAct 执行轨迹，过程透明、掌控感极强。
![ReAct 协同闭环](./.github/assets/pickel_agent_react.png)

### 3. 内置高能开发工具链 (Built-in Development Tools)
安全、受控地扫描目录、内容过滤，并支持高精度的 exact-replace 代码改写工具，确保每一步代码变更都精准无误。
![内置开发工具](./.github/assets/pickel_agent_builtin_tools.png)

### 4. 创新的多轮会话状态与 Token 精准控制 (Session & Context Tracking)
随时通过内建快捷命令控制 Token 使用开销、追溯历史版本、查看当前的 Token 发送窗口大小。
![会话与上下文管理](./.github/assets/pickel_agent_session_context_command.png)

### 5. 模块化 Skill 技能扩展 (Extensible Skills)
动态加载个性化工具、技能包与特殊规则，无缝加载团队沉淀的最佳实践，为特定项目定制专属 Agent。
![模块化 Skill](./.github/assets/pickel_agent_skill.png)

---

## 快速开始

### 前提要求
- Python 3.12+
- Gemini 或 Anthropic API 秘钥（对应模型驱动）

### 1. 安装与同步
```bash
# 克隆仓库
git clone https://github.com/xiesunsun/Pickel-Agent.git
cd Pickel-Agent

# 使用 uv 进行依赖管理与同步
uv sync
```

### 2. 注入凭证与配置
项目从根目录的 `config.yaml` 读取系统配置。您可以将敏感值通过环境变量进行注入。
```bash
# Gemini 凭证配置
export GEMINI_API_KEY="your-gemini-api-key"

# Anthropic 凭证配置
export ANTHROPIC_API_KEY_PICKLE="your-anthropic-api-key"
```
可在 `config.yaml` 中根据需求调整默认大语言模型、安全文件访问级别以及工具白名单。

### 3. 运行交互会话
```bash
# 默认从分层配置发现（~/.pickel + 项目 .pickel / agents）；可选 --config 指定旧 yaml
uv run pickel chat
uv run pickel chat --agent Pickle
uv run pickel sessions
uv run pickel chat --session-id <session-id>
uv run pickel sessions delete <session-id>
# 兼容：uv run pickel chat --config config.yaml
```

---

## CLI 终端快捷指令

在交互会话中，您可以使用以下斜杠命令进行辅助控制：

- `/help` - 显示终端指令帮助列表
- `/context` - 显示当前上下文 Token 消耗与窗口大小
- `/session` - 获取当前多轮会话详细运行状态摘要
- `/clear` - 清空终端屏幕
- `/exit` - 安全保存状态并关闭终端会话

---

## 技术栈

- **Language Core**: Python 3.12+ (使用 `uv` 闪电级依赖管理器驱动)
- **LLM Drivers**: Google GenAI (Gemini 3.0/3.1), Anthropic SDK (Claude/Jupiter)
- **TUI & Console**: Prompt-Toolkit, Typer, Rich
- **Sync Integration**: OpenViking (可选云端/远端适配器)
- **Local Persistence**: SQLite 数据库 (本地存储 `.myopenclaw/sessions.db`)

---

## 项目结构

```
src/
  myopenclaw/               # 核心执行包 (对应 pickel 命令行)
    agents/                 # Agent 行为 prompt 装载与技能检索
    app/                    # 应用程序配置及启动装配根节点 (Composition Root)
    cli/                    # 命令行交互层逻辑与终端 TUI 渲染
    config/                 # YAML 格式系统配置文件加载与环境变量解析
    context/                # 精准 Token 滑动窗口剪裁与编排
    conversations/          # 会话实体抽象模型与持久化层接口
    persistence/            # 基于 SQLite 的会话存储库具体实现
    providers/              # Gemini 与 Anthropic 大语言模型底层驱动与选项封装
    runs/                   # turn 协调调度器与 ReAct 执行策略
    tools/                  # 受控文件系统工具与持久化 PTY 终端 Shell 工具集
    integrations/openviking # 云同步与 session recall 可选扩展适配层
tests/                      # 覆盖 app 装配、配置读取、会话持久化与 shell 操作的完整测试集
```

---

## 测试与验证

项目包含针对运行期、持久层和持久 Shell 工具流的完整单元测试。运行测试以确认环境健康度：
```bash
uv run pytest
```

---

## 发展路线图 (Roadmap)

- [ ] 支持更丰富的本地多模态解析工具 (如 PDF 提取与图片处理)
- [ ] 结合本地向量数据库 (Vector DB) 提供更深度、长周期的本地长记忆检索
- [ ] 适配更多的开源本地模型驱动 (如 Ollama, llama.cpp)
- [ ] 开发轻量级的 React 本地 Web UI 协同控制面板

---

## 许可证

本项目基于 [Apache-2.0](./LICENSE) 许可证开源。
