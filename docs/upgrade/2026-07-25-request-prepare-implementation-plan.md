# 模型请求组装（prepare / Request）— 实施计划

> **For agentic workers:** 按任务顺序实现；步骤用 checkbox 跟踪。  
> **设计依据：** `docs/upgrade/2026-07-25-request-prepare-design.md`  
> **分支建议：** `feature/context-request-prepare-design`（或自该分支拉实现分支）

**Goal:** 将发模型内容的组装收拢为 `prepare` 阶段表；templates 外置；skills 每 turn 可热更；精简 slash（`/model` `/thinking` `/agent` `/new` `/reload`）；新增 `before_request`；为 Recall 留槽。

**Architecture:** ReAct 每 step 只调 `prepare(run, session, …) → Request(ModelContext)`。system 文案来自 templates 文件 + 每 turn discover skills。磁盘资源统一 `/reload`。Agent 仍为定义；Run 为资源袋。

**Tech Stack:** Python 3.12、现有 pytest、prompt-toolkit（slash/选择列表可复用）。

---

## 文件地图（目标）

| 路径 | 职责 |
|------|------|
| `src/pickel/templates/`（包内默认） | 默认 `skills_guidance.md` 等 |
| `src/pickel/context/templates_loader.py` | 合并 包内 / `~/.pickel/templates` / 项目 `.pickel/templates` |
| `src/pickel/context/prepare.py` | 薄编排 `prepare(...)` |
| `src/pickel/context/stages/system.py` | `resolve_system` |
| `src/pickel/context/stages/history.py` | `resolve_history`（projection+window） |
| `src/pickel/context/stages/recalls.py` | `resolve_recalls`（默认空） |
| `src/pickel/context/stages/feedback.py` | `resolve_feedback` |
| `src/pickel/context/stages/tools.py` | `resolve_tools` |
| `src/pickel/context/recall.py` | `Recall` Protocol |
| `src/pickel/hooks/*` | + `before_request` 事件与合并 |
| `src/pickel/runs/strategy/react.py` | 只调 prepare，删私拼 system/tools |
| `src/pickel/agents/skills.py` | 删 `SKILL_USAGE_GUIDANCE` 常量；catalog 格式保留小函数或读模板 |
| `src/pickel/agents/agent.py` | 持 `skills_path`；弱化 `system_instruction` 双源 |
| `src/pickel/app/boot.py` | 不冻死 skills 列表；把 path 交给 Run/Agent |
| `src/pickel/runs/run.py` | 支持 reload 重建资源；Environ 与 reload 保留 model |
| `src/pickel/cli/chat.py` | slash：model/thinking/agent/new/reload |
| `tests/context/test_prepare.py` 等 | 阶段与热更单测 |
| `tests/cli/test_slash_commands.py` | slash 行为 |

**删除/收缩目标：** 对外 `ContextAssembler.assemble` 作为唯一组装入口；ReAct 内 system/tools 手写拼接；Boot 一次 discover 冻在 `Agent.skills` 且永不更新。

**保留：** `ExecutionStrategy` / `ReActStrategy`、`ModelContext`、`Session`、`projection`/`window` 逻辑（迁阶段内）、现有执行层 Hooks。

---

## 阶段总览

```text
P0  templates 外置 + 加载合并（行为与现网文案一致）
P1  prepare 阶段表 + ReAct 接入 + skills 每 turn discover
P2  slash：/model /thinking /agent /new /reload；before_request
P3  Recall 接口 + OV 适配挂槽（无 ReAct 特判）
后话  /fork-agent、pi 式 user prompts、MemoryRecall
```

每阶段结束：`uv run --with pytest python -m pytest tests/ -q` 全绿。

---

## P0 — templates 外置

### Task 0.1: 默认 templates 文件

**Files:**
- Create: `src/pickel/templates/skills_guidance.md`  
  内容 = 现 `SKILL_USAGE_GUIDANCE` 全文（逐字迁移）
- Create: `src/pickel/context/templates_loader.py`
  - `load_templates(home=None, project_root=None) -> dict[str, str]`
  - 键如 `skills_guidance`；合并：包内 < `~/.pickel/templates` < 项目 `.pickel/templates`
- Test: `tests/context/test_templates_loader.py`

- [ ] **Step 1:** 写测试：无覆盖时等于包内默认；用户目录覆盖 `skills_guidance.md` 后生效  
- [ ] **Step 2:** 实现 loader  
- [ ] **Step 3:** `pytest tests/context/test_templates_loader.py -v`  
- [ ] **Step 4:** Commit `feat(context): templates 加载与默认 skills_guidance`

### Task 0.2: compose 走 templates

**Files:**
- Modify: `src/pickel/agents/skills.py`  
  - 删除模块级 `SKILL_USAGE_GUIDANCE` 常量  
  - `compose_system_instruction_parts` 接收 `skills_guidance: str` 或内部调 loader（P0 可先内部调 loader 保持 API 简单）  
- Modify: 所有引用常量的测试（`tests/agents/test_skills.py`、`tests/runs/test_context_usage.py` 等）  
- Test: 断言默认 compose 结果与迁移前一致（可用 fixture 锁字符串）

- [ ] **Step 1:** 失败测试锁定「默认全文与旧常量一致」  
- [ ] **Step 2:** 改 compose + 测全绿  
- [ ] **Step 3:** Commit `feat(context): system 文案改为 templates 文件`

**P0 出口：** 行为字节级兼容默认 skills 引导文案；用户可在 `~/.pickel/templates/skills_guidance.md` 覆盖。

---

## P1 — prepare 阶段表 + skills 每 turn 热更

### Task 1.1: Recall 协议与空实现

**Files:**
- Create: `src/pickel/context/recall.py` — `class Recall(Protocol): def provide(...) -> list[...]`
- Create: `src/pickel/context/stages/recalls.py` — 对 `recall_sources` 循环拼接  
- Test: 空列表不改变 messages

- [ ] 实现 + 单测  
- [ ] Commit `feat(context): Recall 槽位与 resolve_recalls`

### Task 1.2: prepare 与各 stage

**Files:**
- Create: `src/pickel/context/prepare.py`
  ```text
  def prepare(*, run, session, hook_feedback=None, unit_window=None, templates=None, skills=None) -> ModelContext
  ```
- Create stages：`system.py` `history.py` `feedback.py` `tools.py`（+ 已有 recalls）  
- `resolve_system`：behavior + templates + format catalog(skills)  
- `resolve_history`：现 `project_messages` + `apply_window`  
- `resolve_feedback`：现 `append_hook_feedback`  
- `resolve_tools`：现 ReAct tools 映射  
- Modify: `context/__init__.py` 导出 `prepare`  
- Modify: `ContextAssembler` — 标记 deprecated 或改为调用 prepare 子集；**测试全部改调 prepare**

- [ ] 单测：给定假 Session entries + agent 原料，prepare 输出结构正确  
- [ ] 与旧 Assembler 行为对比测试（history+feedback）  
- [ ] Commit `feat(context): prepare 阶段表`

### Task 1.3: Agent / Boot / Run 支持 skills_path 每 turn discover

**Files:**
- Modify: `Agent` — 增加 `skills_path: Path | None`；`skills` 可作缓存字段或不再作为唯一源  
- Modify: `Boot.resolve_agent` — 写入 `skills_path`，可仍 discover 一次作初始  
- Modify: `Run` — 暴露 `skills_path` / `reload_skills()` 或 prepare 内直接 discover  
- **约定：** 每 turn 开始（或每次 prepare）`SkillRegistry.discover(run.agent.skills_path)`  

- [ ] 测试：两次 prepare 之间增删临时 skill 目录，第二次 catalog 变化  
- [ ] Commit `feat(skills): 每 turn discover 支持热更`

### Task 1.4: ReAct 只调 prepare

**Files:**
- Modify: `runs/strategy/react.py`  
  - 删除 `SystemContent.from_text(run.agent.system_instruction)`  
  - 删除手写 tools 列表构建  
  - `model_context = prepare(run=run, session=session, hook_feedback=..., unit_window=run.unit_window)`  
- Modify: 相关 runs 测试  

- [ ] `pytest tests/runs -q`  
- [ ] Commit `refactor(react): 经 prepare 组装 Request`

### Task 1.5: P1 验收

- [ ] 全量 `pytest tests/ -q`  
- [ ] 手工：改 `~/.pickel/templates/skills_guidance.md` 后新 turn system 变化（若已接 loader 每 prepare 读）  
- [ ] 手工：不重启进程，增 skill 目录，下一 user turn catalog 含新 skill  

**P1 出口：** 组装唯一路径；skills 可热更；默认文案仍正确。

---

## P2 — slash 与 before_request

### Task 2.1: before_request hook

**Files:**
- Modify: `hooks/events.py` — `BeforeRequestEvent`（含拟发送 ModelContext 快照字段或引用）  
- Modify: `hooks/decisions.py` — `BeforeRequestDecision`（可选替换 context / feedback）  
- Modify: `hooks/lifecycle.py` — `before_request` 分发与合并策略（后写覆盖或明确文档）  
- Modify: `prepare` 末尾或 ReAct 在 generate 前调用 hooks  
- Test: handler 可改 system 文本  

- [ ] Commit `feat(hooks): before_request`

### Task 2.2: `/model` 与 `/thinking`

**Files:**
- Modify: `cli/chat.py` `_handle_command`  
  - `/model` 无参：列出可用 `provider/model`（重读 models.json + 已配置 providers）；有参则解析并 `run.apply_environ_model`  
  - `/thinking <level>`：写 Environ.provider_options  
- 需 ChatLoop 持有 `AppConfig` 或可 resolve model 的引用（Boot/app_config 传入）  
- Test: `tests/cli/test_slash_commands.py` mock Run/Environ  

- [ ] Commit `feat(cli): /model 与 /thinking`

### Task 2.3: `/agent` 与 `/new`

**Files:**
- Modify: `cli/chat.py`  
  - `/agent` 无参：列出 `app_config.agents` 的 id，**选择列表**（prompt_toolkit 或编号选择）  
  - `/agent <id>`：校验 id → `Session.create(agent_id=id, cwd=...)` 空会话 → `boot.build_run(agent_id=id)` 换 `_run` / session / session_service  
  - `/new`：同 agent_id 新 Session，Run 可保留  
- 旧 session 不删  
- Test：切换后 session.agent_id 与空历史；旧 id 仍可 resume  

- [ ] Commit `feat(cli): /agent 与 /new`

### Task 2.4: `/reload`

**Files:**
- Modify: `runs/run.py` 或 Boot 增加 `reload(run, app_config) -> Run`  
  范围严格按设计 §6.3：  
  - Config.load 再合并  
  - re-discover skills、templates  
  - 重读当前 agent.yaml + AGENT.md  
  - 重建 tools/workspace/provider（**保留 Environ 的 model 选择**）  
  - 插件后话可空实现  
- Modify: `cli/chat.py` `/reload` 调用并打印摘要  
- Test：改 agent.yaml tools 后 reload，白名单变；session_id 不变  

- [ ] Commit `feat(cli): /reload 磁盘资源热重载`

### Task 2.5: `/help` 与 P2 验收

- [ ] 更新 help 文案为设计主列表  
- [ ] 全量 pytest  
- [ ] 手工清单：model/thinking/agent 列表/new/reload  

**P2 出口：** 精简 slash 可用；`/agent` 支持 id 与列表；`/reload` 范围符合设计。

---

## P3 — Recall + OpenViking

### Task 3.1: SessionRecallProvider → Recall

**Files:**
- Modify: `context/session_recall.py` / OV 实现实现 `Recall.provide`  
- Modify: Boot/`prepare` 注入 `recall_sources` 列表（OV 开则 append）  
- 确保 ReAct **无** OV 特判  

- [ ] 单测 mock Recall 注入消息  
- [ ] Commit `feat(context): OpenViking 经 Recall 槽注入`

### Task 3.2: P3 验收

- [ ] OV 关闭时与 P2 行为一致  
- [ ] 全量 pytest  

---

## 测试策略

| 类型 | 做法 |
|------|------|
| 单元 | prepare 各 stage、templates 合并、discover 热更 |
| CLI | CliRunner + mock Boot/Run；slash 解析 |
| 回归 | 全量 pytest；重点 runs/context/cli/agents |
| 手工 | 同进程改 skill 目录、templates、`/reload`、`/agent` 列表 |

隔离：`PICKEL_HOME` + tmp 项目根，勿写用户真配置（除开发者自测）。

---

## 风险与顺序

```text
0.x templates
  → 1.2 prepare（可先接旧 compose）
  → 1.3 skills 热更
  → 1.4 ReAct
  → 2.x slash 依赖 Run.reload / AppConfig 引用
  → 3.x Recall
```

| 风险 | 缓解 |
|------|------|
| system 字符串微调导致 snapshot 测碎 | P0 锁默认文件内容 = 旧常量 |
| ChatLoop 状态多（run/session/config） | 小函数 `switch_agent` / `reload_run` 集中突变 |
| `/reload` 半失败 | 校验失败不替换 run |
| prompt_toolkit 选择列表体验 | 先做编号列表，再增强 UI |

---

## 提交信息约定

- `feat(context):` / `feat(cli):` / `feat(hooks):` / `refactor(react):`  
- 中文说明可附正文  
- 小步，对应 Task  

---

## 验收清单（全部完成后）

- [ ] 默认 skills 引导文案与升级前一致（无用户覆盖时）  
- [ ] ReAct 无私拼 system/tools；只经 prepare  
- [ ] 同进程：增 skill 目录，下一 turn catalog 更新  
- [ ] `/model` `/thinking` 改 Environ，同 Session  
- [ ] `/agent` 无参列表 / 有参 id → **新开空 Session**  
- [ ] `/new` 同 agent 空 Session  
- [ ] `/reload` 范围符合设计；Session/agent_id 不变；Environ model 保留  
- [ ] `before_request` 可测  
- [ ] Recall 默认可空；OV 不进 ReAct 特判  
- [ ] 无 `/reload-xxx` 分项命令  
- [ ] pytest 全绿  

---

## 执行方式

1. **Subagent 逐 Task**  
2. **本会话顺序执行**  

设计已定：`docs/upgrade/2026-07-25-request-prepare-design.md`。  
实现勿扩大范围：不做 fork-agent、不做 pi 用户 prompts、不做记忆产品。
