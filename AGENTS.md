# Code Agent Rule 

## 开发环境

### 系统信息
- **操作系统**: macOS 26
- **CPU架构**: Apple Silicon M5
- **内存**: 16GB
- **存储**: 512GB SSD
- **终端**: zsh (配置: ~/.zshrc)

### 开发工具
- **版本控制**: Git 2.52.0
- **包管理器**: Homebrew 5.0.11

### 编程语言
- **Python**: 3.12.7 (通过  uv 管理python 版本和项目安装python包)
- **Node.js**: v22.21.0 (通过 nvm 管理)
- **Python代码格式**: black

### 项目管理
- **代码组织**: Monorepo 架构
- **分支策略**: Git Flow
  - main: 生产代码
  - develop: 开发集成
  - feature/*: 功能开发
  - hotfix/*: 紧急修复

## 编码原则
- 遵循Unix Philosophy
- Keep simple and keep stupid
- 变量名、实体名、方法名、包名等等命名需要直接、易懂通俗
- 架构设计尽量避免跨层依赖，注意解耦，目的确保项目架构能够快速迭代演进
- 良好文档归档习惯，遵循软件工程开发文档撰写标准，尽量使用架构图，表格、html网页等信息密度高的可阅读性强的载体呈现，先写文档后做开发，开发验收完成后校对文档，确保文档和实现一致
- 遵循计算机软件开发第一公理，“项目的需求都已经有人早已想到，并且实现好的了”，所以分析需求、设计方案，解决问题时，尽可能深度调研，一定能够获得类似的参考代码和解决方案

## 设计文档路由（渐进式披露，强制）

不要在每次任务中读取全部 `docs/`。先根据改动范围命中文档，只完整读取命中的文档及其直接引用；没有命中则以本文件和现有代码为准。

### 文档优先级

发生冲突时按以下顺序处理：

1. 用户在当前任务中的明确要求；
2. 本 `AGENTS.md`；
3. [`Agent Runtime 重构命名约束`](docs/upgrade/2026-08-10-agent-runtime-naming.md)；
4. 命中的领域设计文档；
5. 历史实现计划和旧代码。

2026-08-10 之前的 Runtime、Session、Context、Observability 实现稿均视为历史资料；其中的 `Run`、Strategy、`SessionService`、`ContextAssembler` 和旧表结构不得作为当前实现依据。

### 第一层：何时读取命名合同

任务涉及下列任一内容时，修改代码或设计前必须完整读取 [`docs/upgrade/2026-08-10-agent-runtime-naming.md`](docs/upgrade/2026-08-10-agent-runtime-naming.md)：

- 新增、删除或重命名实体、方法、字段、事件、模块；
- 修改 `Agent`、`Run`、`Turn`、`Step`、`Session`、`Operation`、`Runtime` 的职责；
- 重构 `ReActStrategy`、`ExecutionStrategy`、`ContextAssembler` 或 `prepare()`；
- 设计持久化状态、Operation 恢复、Artifact、多模态或多 Agent；
- 评审代码是否出现同义类型、资源袋或上帝对象。

普通缺陷修复、文案修改、测试数据调整且不改变上述合同的，不必读取该文档。

### 第二层：按领域继续读取

读取命名合同后，仅在任务命中对应领域时继续读取：

| 任务范围 | 必读文档 | 用途 |
| --- | --- | --- |
| Session 树、消息持久化、Context 投影、Artifact、多 Agent 关系 | [`数据库实体设计`](docs/upgrade/2026-07-12-db-entities.md) | 约束 Runtime v9 的持久化事实、引用和原子提交 |
| Tool Loop、Runtime/Host 边界、实体与方法命名 | [`Agent Runtime 重构命名约束`](docs/upgrade/2026-08-10-agent-runtime-naming.md) | 约束当前组件职责与唯一术语 |
| Config、Settings、Agent Package Snapshot | [`配置系统升级设计`](docs/upgrade/2026-07-25-config-system-design.md) | 只参考 Pickel 设置来源；Runtime 实体与数据库名称以三份当前合同为准 |
| Operation、AgentRunState、Tool 恢复 | [`Operation 持久化与恢复模型`](docs/upgrade/2026-08-11-operation-recovery-model.md) | 约束 Operation 接受事务、状态引用、Package 绑定和未知副作用恢复语义 |
| Anthropic 请求与响应映射 | [`Anthropic Provider 设计`](docs/superpowers/specs/2026-04-21-anthropic-provider-design.md) | 只参考 Anthropic 协议语义；其中旧 Strategy/Run 描述已失效 |

### 使用要求

- 开始实现前，在工作计划中写明本任务命中了哪些文档；未命中的文档不要为了“了解全貌”继续展开。
- 领域文档与代码不一致时，先判断文档描述的是历史现状还是目标合同；不能静默选择其中一方。
- 设计仍未对齐时只在对话中讨论，不把推测写成新的权威文档。
- 实现验收后校对本任务命中的文档；更新原文，不另建同主题 v2/v3。

## 文档约束（强制）

写仓库内文档（`docs/**`、设计稿、升级说明等）必须遵守：

1. **禁止随便写文档**  
   - 用户未明确要求「写入仓库 / 落到 docs / 写设计文档」时，**默认只在对话里说明**，不要主动新建或堆砌 markdown。  
   - 不要为「看起来完整」而写配套文、速查表、总览+分册重复内容。

2. **一份主题一份文档**  
   - 范围要窄、目的要单一（例如「仅数据库实体」就不要夹带 Runtime / 管道 / 学习笔记）。  
   - 禁止同一结论复制成多份文件互相引用、反复修订却不删旧稿。

3. **先对齐再落盘**  
   - 设计未与用户对齐前，不写「定稿」进仓库。  
   - 用户拍板后，**更新或替换**既有文档，而不是再开一篇 v2/v3 并行存在。

4. **写清楚、写短**  
   - 用表格和必要图，少空话、少术语堆砌、少「修订记录流水账」。  
   - 命名直接；读者应能快速看懂「有什么实体、什么字段、是否落库」。

5. **改完要收拾**  
   - 用户要求只保留某份文档时，删除过时/重复文件，避免 `docs/` 变成草稿堆。  
   - 文档与实现不一致时，以校对后的文档为准并改到一致。

6. **对话回答 ≠ 仓库文档**  
   - 分析、对比、探讨可以在对话中完成；只有需要长期约束实现时才写入 `docs/`。


## 输出建议
1. 代码注释用中文
2. 提交信息用中文
3. 错误提示和说明用中文
4. 保持代码、命令、路径等技术标识符不变
