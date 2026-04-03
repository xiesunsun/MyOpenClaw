---
name: skill-creator
description: 用于创建或更新 Skill 的指导文档。当用户希望为 Agent 新增专项能力、封装特定工作流，或改进已有 Skill 时使用。触发词包括：创建技能、新建 Skill、更新 Skill、添加工具、封装工作流等。
---

# Skill Creator

本文档为 Agent 提供创建和维护 Skill 的完整指导。

## 关于 Skill

Skill 是模块化、自包含的文件夹，通过提供专项知识、工作流和工具集成来扩展 Agent 的能力。可以将其理解为特定领域的"操作手册"——它将 Agent 从通用助手转变为具备程序性知识的专项助手。

### Skill 能提供什么

1. **专项工作流** — 特定领域的多步骤操作流程
2. **工具集成** — 特定文件格式或 API 的操作说明
3. **领域知识** — 业务特定的知识、数据结构、业务逻辑
4. **复用资源** — 脚本、参考文档和资产文件

---

## 核心原则

### 简洁优先

上下文窗口是有限资源，Skill 与系统提示、对话历史、用户请求共享这一空间。

**默认假设：Agent 已经很聪明。** 只添加 Agent 本身不具备的上下文。对每条信息追问："Agent 真的需要这个解释吗？"以及"这段内容值得占用这些 token 吗？"

**优先用简洁示例替代冗长说明。**

### 自由度匹配任务脆弱性

根据任务的刚性与变化性，设置合适的指令层级：

| 自由度 | 形式 | 适用场景 |
|--------|------|----------|
| **高** | 文字说明 | 多种方案均有效、决策依赖上下文、启发式引导 |
| **中** | 伪代码 / 带参数脚本 | 有偏好模式、允许一定变化 |
| **低** | 具体脚本、少量参数 | 操作脆弱易出错、必须保持一致性、顺序固定 |

---

## Skill 的结构

```
skill-name/
├── SKILL.md          （必需）主入口，包含元数据与指导说明
└── resources/        （可选）
    ├── scripts/      可执行代码（Python / Bash 等）
    ├── references/   参考文档，按需加载
    └── assets/       输出所用文件（模板、图片、字体等）
```

### SKILL.md（必需）

每个 SKILL.md 由两部分组成：

**Frontmatter（YAML）** — 只允许以下字段：
- `name`：Skill 名称（必需）
- `description`：**触发机制的核心**（必需）
- `metadata`：可选的附加键值对

> ⚠️ 不允许出现其他 frontmatter 字段，`quick_validate.py` 会对此进行检查。

description 写作要求：
- 必须同时包含"做什么"和"何时用"
- 所有触发条件写在这里，不要写在 body 中（body 只在触发后才加载）
- 不超过 1024 字符，不包含 `<` `>` 符号
- 示例：`"处理 PDF 文件的完整工具集，支持文本提取、合并、拆分、旋转、加水印。当用户需要对 .pdf 文件进行任何操作时使用。"`

**Body（Markdown）**
- Agent 使用该 Skill 的操作说明
- 只包含 Agent 需要的信息，无需解释创建背景或测试过程

### scripts/（可选）

可执行代码，适用于需要确定性执行或反复重写的任务。

- **何时添加**：同类代码被反复重写时；需要确定性结果时
- **优点**：节省 token，结果确定，无需加载即可执行
- **注意**：脚本写完后必须实际运行测试，确认无 bug

### references/（可选）

按需加载到上下文的参考文档。

- **何时添加**：Agent 工作过程中需要查阅的背景资料
- **示例**：`references/schema.md`（数据库结构）、`references/api_docs.md`（API 文档）
- **最佳实践**：若文件超过 10k 词，在 SKILL.md 中注明搜索关键词
- **避免重复**：信息只存在于一处，要么在 SKILL.md，要么在 references 文件

### assets/（可选）

不需要加载到上下文，而是直接用于输出的文件。

- **示例**：模板文件、图片、字体、样板代码目录

### 不应包含的内容

**不要创建**以下文件：README.md、INSTALLATION_GUIDE.md、CHANGELOG.md 等辅助说明文档。

---

## 渐进式披露设计原则

Skill 采用三级加载机制：

1. **元数据**（name + description）— 始终在上下文中（约 100 词）
2. **SKILL.md body** — Skill 触发后加载（建议 < 500 行）
3. **resources/ 文件** — 由 Agent 按需读取（无大小限制）

### 内容拆分模式

**模式一：高层指引 + 引用**

```markdown
# PDF 处理
## 快速开始
使用 pdfplumber 提取文本：[代码示例]
## 进阶功能
- **表单填写**：参见 [references/forms.md](references/forms.md)
```

**模式二：按领域组织**

```
bigquery-skill/
├── SKILL.md
└── references/
    ├── finance.md
    ├── sales.md
    └── product.md
```

**模式三：条件细节**

```markdown
## 高级功能
- **修订追踪**：参见 [references/track-changes.md](references/track-changes.md)
```

**重要规则：**
- references 文件只深一层，所有文件从 SKILL.md 直接引用
- 超过 100 行的 references 文件，顶部加目录

---

## Skill 创建流程

按顺序执行以下步骤：

1. 理解 Skill（收集具体使用示例）
2. 规划可复用内容（脚本、参考文档、资产）
3. 运行 `init_skill.py` 初始化目录
4. 编写 Skill 内容
5. 运行 `quick_validate.py` 验证结构

---

### 第一步：理解 Skill

通过以下问题收集信息（逐步追问，不要一次性全问）：

- "这个 Skill 需要支持哪些功能？"
- "能给几个具体的使用例子吗？"
- "用户会用什么样的方式触发这个 Skill？"
- "Skill 应该创建在哪个目录下？"

**本步骤结束条件**：清楚知道 Skill 需要支持哪些功能。

---

### 第二步：规划可复用内容

分析每个具体示例：

| 场景 | 分析结果 | 应添加的资源 |
|------|----------|--------------|
| "帮我旋转这个 PDF" | 每次都要重写相同代码 | `scripts/rotate_pdf.py` |
| "查询今天登录用户数" | 每次都要重新查找表结构 | `references/schema.md` |
| "构建一个 Todo 应用" | 每次都要写相同的 HTML 样板 | `assets/frontend-template/` |

**本步骤结束条件**：明确列出需要创建的资源文件清单。

---

### 第三步：初始化目录（`init_skill.py`）

使用 `scripts/init_skill.py` 创建标准目录结构：

```bash
# 基础用法
python scripts/init_skill.py <skill-name> --path <输出目录>

# 同时创建资源目录
python scripts/init_skill.py my-skill --path skills/ --resources scripts,references

# 创建资源目录并生成示例文件
python scripts/init_skill.py my-skill --path skills/ --resources scripts,references --examples
```

脚本会自动完成：
- 创建 `skill-name/` 目录
- 生成带 TODO 占位符的 `SKILL.md` 模板
- 按需创建 `scripts/`、`references/`、`assets/` 子目录
- 输出后续操作提示

**Skill 命名规则（脚本会自动规范化）：**
- 只使用小写字母、数字和连字符
- 优先使用动词开头的短语（如 `pdf-editor`、`sql-query`）
- 工具相关时加命名空间（如 `gh-pr-review`）
- 不超过 64 个字符

---

### 第四步：编写 Skill 内容

#### 先实现可复用资源

从 scripts/、references/、assets/ 文件开始，再写 SKILL.md。

- 编写的脚本**必须实际运行测试**，确认无 bug 且输出符合预期
- 若有多个相似脚本，测试有代表性的样本即可
- 若使用了 `--examples`，删除不需要的示例占位文件

#### 编写 SKILL.md

**写作风格：** 始终使用祈使句 / 动词原形。

**Frontmatter 约束（`quick_validate.py` 会检查以下内容）：**

```yaml
---
name: skill-name          # 必需；hyphen-case；≤64 字符
description: |            # 必需；≤1024 字符；不含 < >
  说明做什么 + 何时触发
  触发词：xxx、yyy、zzz
metadata:                 # 可选
  key: value
---
```

**Body 要求：**
- 控制在 500 行以内
- 详细参考资料放进 references/ 文件，并在 SKILL.md 中注明路径和加载时机
- 不写"本技能的使用场景"等废话章节

---

### 第五步：验证结构（`quick_validate.py`）

```bash
python scripts/quick_validate.py <skill目录路径>

# 示例
python scripts/quick_validate.py skills/my-skill
```

验证内容：
- SKILL.md 存在且 frontmatter 格式正确
- `name` 和 `description` 字段存在且类型正确
- `name` 符合 hyphen-case 规则，不超过 64 字符
- `description` 不含 `<` `>`，不超过 1024 字符
- frontmatter 中无不允许的字段（只允许 `name`、`description`、`metadata`）

验证失败时，修复报告中的问题后重新运行。

---

## 迭代改进

在实际使用中发现问题后：

1. 在真实任务中使用该 Skill
2. 观察执行中的困难或低效之处
3. 确定需要更新的地方
4. 修改后重新运行 `quick_validate.py`

**常见改进方向：**

| 现象 | 改进方向 |
|------|----------|
| description 触发不准确 | 补充触发词或明确排除场景 |
| Agent 反复询问相同信息 | 补充到 references/ 文件 |
| 脚本使用有误 | 在 SKILL.md 中添加使用示例 |
| SKILL.md 过长 | 将详细内容拆分到 references/ 文件 |