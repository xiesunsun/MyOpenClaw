# V1a Skill 自管理设计稿

日期：2026-07-26。前置：T1 工具总线、E1 extension 宿主、S2 沙箱（均已完成）。调研：`docs/upgrade/2026-07-26-tools-sandbox-research.md` §3.3。

目标：给 agent 一条**受控的**自我能力写入通道——skill frontmatter 扩展 + `skill_manage` 工具 + 暂存审批 + 内容护栏。

## 1. 范围与拆分

原 V1 覆盖三个独立子系统，本稿只做第一个：

| 子项目 | 内容 | 本期 |
| --- | --- | --- |
| **V1a Skill 自管理** | frontmatter 扩展、`skill_manage`、暂存审批、内容护栏 | ✅ |
| V1b 版本模型与回滚 | git SHA 回落版本号、代码目录按版本隔离 + 数据目录持久、旧版本宽限期回收 | ❌ 现只有 2 个内置 extension，需求不真实 |
| V1c 参数 schema 演进 shim | resume 旧 session 时迁移旧工具参数（Pi 的 `prepareArguments`） | ❌ 体量小，适合搭车后续变更 |

**本期不做**：skill 生命周期自动迁移（active→stale→archived 的定时 Curator）；skill 依赖解析与安装；远程 skill 源；skill 版本回滚。

## 2. 决策记录

| 问题 | 决策 | 理由 |
| --- | --- | --- |
| 形态 | core 内 `SkillStore` + `skill_manage` 工具 + `/skills` 命令 | skill 是 core 概念（`SkillRegistry` 已在 core）；做 extension 需要 E1 没有的 `register_command` 扩展点 |
| 审批 | 默认暂存待审（`skills.write_approval: true`） | 与 S2「沙箱是自修改的许可证」一致；可配置关闭 |
| 生命周期 | 只加 `status` 字段，不做自动迁移 | 目前 5 个 skill 不构成膨胀，Curator 需要使用计数与调度器 |
| 护栏 | 写入前静态扫描，默认开 | 挡明显自伤；与 S1 危险命令拦截同定位——挡君子不挡对抗 |

## 3. 架构

```
src/pickel/agents/skills.py        # SkillManifest 扩展字段 + catalog 过滤（改）
src/pickel/skills/store.py         # SkillStore：写入/暂存/审批的唯一入口（新）
src/pickel/skills/guard.py         # 内容护栏静态规则（新）
src/pickel/tools/skill_manage.py   # SkillManageTool（新）
src/pickel/cli/chat.py             # /skills 命令（改）
```

`SkillStore` 是唯一写入口——工具与 CLI 命令都经它，审批状态与落盘规则只有一处实现。

### 3.1 frontmatter 扩展

全部可选，缺省即现状，向后兼容：

```yaml
---
name: image-generator          # 现有
description: ...               # 现有
version: 1.2.0                 # 新：缺省回落到该 skill 目录最后一次 git commit 的短 SHA
status: active                 # 新：active | stale | archived（缺省 active）
required_env: [GEMINI_API_KEY] # 新：所需环境变量
allowed_tools: [shell_exec]    # 新：声明用途；本期不强制，仅进 catalog 供模型参考
---
```

`SkillManifest` 增对应字段。解析失败（如 `status` 写了未知值）→ 该字段回落默认值并记 warning，不丢弃整个 skill。

### 3.2 catalog 影响

`skills_catalog` 的装配（`prepare` 每 turn re-discover）按新字段过滤与标注：

- `status: archived` → 完全排除，不进 catalog。
- `status: stale` → 进 catalog，名字后标 `(stale)`。
- `required_env` 有缺失变量 → 进 catalog 但标 `(unavailable: 需要 GEMINI_API_KEY)`，模型据此不误用。检查的是 pickel 进程的环境（`os.environ`），不是沙箱内 shell 的。
- `version` 进 catalog 行尾，便于模型与日志辨识。

### 3.3 SkillStore

```python
@dataclass(frozen=True)
class SkillWriteRequest:
    action: str            # "create" | "patch" | "delete"
    skill_name: str
    content: str = ""      # create：整篇 SKILL.md
    old_text: str = ""     # patch：被替换的唯一片段
    new_text: str = ""     # patch：替换成什么

@dataclass(frozen=True)
class SkillWriteOutcome:
    applied: bool          # True=已落盘；False=已暂存待审
    pending_id: str | None
    path: Path | None
    message: str
```

`SkillStore.submit(request) -> SkillWriteOutcome` 的流程：

```
1. 校验：skill_name 合法（[a-z0-9-]+，无路径穿越）、action 已知、必填字段齐
2. 计算目标内容：
     create → content
     patch  → 读现有 SKILL.md，old_text 必须唯一命中一次，替换
     delete → 无内容
3. 护栏扫描（delete 跳过）：命中 → 抛 SkillGuardError，不落盘不暂存
4. 审批分支：
     write_approval=False → 直接写 <skills_path>/<name>/SKILL.md，applied=True
     write_approval=True  → 写 ~/.pickel/pending/skills/<pending_id>.json，applied=False
```

pending 记录是单个 JSON（`{id, action, skill_name, content|old_text/new_text, created_at, agent_id}`），`pending_id` 取 `uuid4().hex[:8]`。

审批操作：

- `list_pending() -> list[PendingWrite]`
- `diff(pending_id) -> str`——unified diff（现有内容 vs 目标内容；create 对空文件，delete 对空目标）
- `approve(pending_id) -> Path`——落盘并删 pending 记录
- `reject(pending_id) -> None`——只删记录

### 3.4 skill_manage 工具

```
skill_manage(action, skill_name, content?, old_text?, new_text?)
```

- 描述里写明：写入默认进待审队列，需要用户批准后才生效；patch 是修改既有 skill 的推荐方式（比重写整篇安全）。
- 返回 content：已落盘 → 路径；已暂存 → `Pending approval (id: ab12cd34). The user must run /skills approve ab12cd34.`；护栏拒绝 → is_error + 命中的规则名。
- metadata：`applied` / `pending_id` / `path` / `action`。
- 白名单：`agents/Pickle/agent.yaml` 增 `skill_manage`。

### 3.5 /skills 命令

`/skills`（= `/skills pending`）、`/skills diff <id>`、`/skills approve <id>`、`/skills reject <id>`。

沿用 `cli/chat.py` 既有命令分发风格；输出用现有 Rich 面板。approve 后提示「下一个 turn 生效」（skill 是每 turn re-discover，无需 reload）。

### 3.6 内容护栏

写入前扫描目标内容，命中即拒（`skills.guard: false` 可关）：

| 规则 | 命中形状 |
| --- | --- |
| 凭据收集 | 同时出现凭据路径（`~/.ssh`、`.env`、`credentials`、`.aws`）与读取动作（`cat`/`read`/`open`） |
| 凭据外泄 | 出现 `curl`/`wget`/`requests.post` 且同一行/邻近行带 `API_KEY`/`TOKEN`/`SECRET` 等变量引用 |
| prompt injection | `ignore (all )?previous instructions`、`disregard your (system )?prompt`、`你现在是`+`忽略之前` 等模式 |
| 沙箱逃逸引导 | 出现 `sandbox.allow_disable`、`dangerously_disable_sandbox`、`sudo tee /etc/apparmor.d` |

- 只挡明显自伤：正则级别，不做语义分析（与 S1 危险命令拦截同定位）。
- 拒绝时返回命中的规则名与匹配片段，让模型知道改哪。

### 3.7 与 S2 沙箱的关系

- `skill_manage` 跑在 pickel 进程内（工具层），不经 shell，因此**不受 bwrap 边界限制**——这是有意的：写入受审批而非受文件系统限制。
- `~/.pickel/pending/` 在沙箱内是 tmpfs（S2 的 deny_read 清单含 `~/.pickel`），沙箱内的 shell 看不见也改不了待审队列——agent 无法用 `shell_exec` 自我批准。
- `.agent/skills/` 在 workspace 内、沙箱内可写：agent 理论上能用 `shell_exec` 绕开 `skill_manage` 直接写 skill 文件。**本期接受**（遗留取舍 1）。封堵手段是把 skills 目录加进 S2 的 self-protect 只读清单——`approve` 走进程内写入不过 bwrap，所以不受影响，技术上可行；代价是用户自己在沙箱 shell 里也改不了 skill。先观察真实使用再决定。

## 4. 错误处理汇总

| 场景 | 行为 |
| --- | --- |
| skill_name 非法（含 `/`、`..`、大写） | is_error + 说明合法形状 |
| `patch` 的 `old_text` 未命中或命中多次 | is_error + 实际命中次数 |
| `patch`/`delete` 的 skill 不存在 | is_error + 现有 skill 名列表 |
| 护栏命中 | is_error + 规则名 + 匹配片段 |
| `approve` 的 id 不存在 | CLI 报错 + 现有 pending id 列表 |
| pending 目录不可写 | 抛错，工具转 is_error |
| skills_path 未配置 | is_error（该 agent 无 skill 目录） |

## 5. 测试计划

| 层 | 覆盖 |
| --- | --- |
| manifest 单测 | 新字段解析、缺省回落、坏值 warning 不丢 skill、version 回落 git SHA |
| catalog 单测 | archived 排除、stale 标注、required_env 缺失标注、version 进行尾 |
| store 单测 | create/patch/delete 三动作、patch 唯一性校验、审批开/关两条路径、pending 落盘与 diff/approve/reject |
| guard 单测 | 四类规则各命中一例 + 正常 skill 内容放行（误杀回归） |
| 工具单测 | 返回文案与 metadata、错误分支 |
| CLI 单测 | `/skills` 四个子命令的分发与输出 |
| 手动验收 | 真会话：让 agent 建一个 skill → 待审 → `/skills diff` → `approve` → 下一个 turn catalog 里出现 |

## 6. 遗留取舍

1. **`shell_exec` 能绕开审批直接写 skill 文件**——本期接受，见 §3.7。
2. **护栏是正则不是语义分析**——对抗性写法绕得过；它的定位是挡「模型无意中写出危险 skill」，不是挡「模型蓄意逃逸」。
3. **版本号回落 git SHA 需要 skill 目录在 git 仓库内**——不在仓库内时 `version` 留空，catalog 不显示版本。
4. **`allowed_tools` 声明但不强制**——强制需要在工具激活集里按 skill 上下文动态收窄，那是 T1 激活集的下一步演进，本期只记录声明。
5. **生命周期字段无自动迁移**——`stale`/`archived` 只能手写或由未来的 Curator 写。
