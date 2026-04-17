---
name: skill-creator
description: Use when creating a new repo-local skill, repairing an existing skill's structure, or improving how the agent discovers and invokes skills from .agent/skills.
---

# Skill Creator

为 `.agent/skills` 中的技能提供统一结构和写法，避免“能被扫描到，但 Agent 不会触发或不会调用”的情况。

## 目标

- 用稳定的 metadata 让 Agent 愿意打开这个 skill
- 用面向 Agent 的正文告诉它该怎么执行
- 用可直接运行的脚本取代硬编码 `sys.path` 的教程式片段

## 标准结构

```text
skill-name/
├── SKILL.md
├── scripts/        # 可选，确定性执行逻辑
├── references/     # 可选，按需加载的长文档
└── assets/         # 可选，模板或静态资源
```

- 统一使用大写文件名 `SKILL.md`
- 不额外创建 `README.md`、`CHANGELOG.md` 之类的旁路文档
- `scripts/` 里的脚本应能被 Agent 直接调用，而不是要求先修改 `sys.path`
- 对脚本型 skill，主执行示例直接写脚本绝对路径，不要把相对路径作为默认用法

## Frontmatter 规则

```yaml
---
name: skill-name
description: Use when ...
metadata:
  key: value
---
```

- `name` 必须是 hyphen-case，长度不超过 64
- `description` 只写触发条件，不写流程摘要
- `description` 最好以 `Use when ...` 开头
- 把用户意图、症状、文件类型、关键词写进 `description`
- 不要在 `description` 里塞“先做 A 再做 B”这类工作流

示例：

```yaml
# 差：描述了流程，触发性弱
description: 生成图片并保存到本地，调用内置脚本完成

# 好：只描述何时需要这个 skill
description: Use when users ask to generate a new image file from a text prompt in the current workspace.
```

## 正文写法

- 直接对 Agent 下指令，不写面向人的产品介绍
- 明确说明：
  - 什么时候触发
  - 先收集哪些参数
  - 应调用哪个脚本或读取哪个参考文件
  - 默认值是什么
  - 失败时如何处理和如何向用户说明
- 详细资料放进 `references/`
- 可重复、易出错、需要稳定输出的步骤放进 `scripts/`
- 如果 skill 脚本不保证位于当前 workspace 内，正文直接给绝对路径命令，不要让 Agent 自己判断 cwd

## 推荐流程

1. 收集 2 到 3 个会触发该 skill 的真实请求
2. 判断是否需要脚本来承载重复逻辑
3. 优先用 `uv run python` 运行脚本；对脚本型 skill，正文主示例直接写脚本绝对路径
4. 先写脚本和参考资料，再写 `SKILL.md`
5. 用同一种方式运行 `scripts/quick_validate.py <skill-dir>`
6. 实际执行至少一次脚本，确认不是只“看起来合理”

## 何时加脚本

适合放到 `scripts/`：

- API 调用
- 文件转换
- 需要稳定参数和输出格式的命令
- Agent 每次都可能写错的样板逻辑

不适合放到脚本：

- 纯策略说明
- 很短、不会重复、没有确定性收益的内容

## 常见修复

- 把 `skill.md` 统一改为 `SKILL.md`
- 把模糊或宣传式 description 改成 `Use when ...`
- 删除正文里的绝对路径导入示例
- 把“import 某函数再手调”的方式改成 CLI 示例
- 把依赖 cwd 的相对脚本路径改成绝对路径
- 去掉与当前官方 API 不一致的模型名或参数

## 验证

```bash
uv run python scripts/quick_validate.py <skill目录路径>
```

通过后再做一次真实 smoke test。对脚本型 skill，只过结构校验不算完成。
