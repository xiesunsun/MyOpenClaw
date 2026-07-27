# CLI 渲染块模型增强方案（E3.1）

**状态**：已实施（代码在 `test/e2e`；待真机抽检）  
**日期**：2026-07-27  
**基线**：`feat/observe-platform` / 当前 `test/e2e` 所基于的 E3 无边框渲染  
**范围**：仅 CLI 展示层（`cli/render/*` + `event_renderer`）  
**不在范围**：TUI、Runtime 事件合同变更、Session/trace 真源、交互式展开

**关联**

| 文档 / 代码 | 关系 |
|-------------|------|
| `2026-07-26-runtime-events-and-cli-ui-design.md` | 事件与 UI 解耦红线 |
| `2026-07-26-events-e3-implementation-plan.md` | E3 现状：流式预览 + 终态 MD 双渲、工具固定 2 行改写 |
| `src/pickel/cli/render/{stream,tool,message}.py` | 实施落点 |
| `src/pickel/cli/event_renderer.py` | 分派入口 |

---

## 1. 问题

端到端实测（`uv run pickel`）暴露两类体感问题：

| 现象 | 根因 |
|------|------|
| 最终回复出现两遍（白字 `-` 列表 + MD `•` 列表） | E3 设计：流式是预览，`AssistantMessageEvent` 再 `Markdown` 重渲，且**两份都留在屏上** |
| `⏺ shell_exec` 出现两行 | 工具 started 打 label；completed 固定 `CURSOR_UP 2` 改写；折行/终端差异时盖不干净 |

次要：参数压成一行宽截断、结果压成约 180 字单行，**名/参/结果「有」但常不可读**。

事件层只各发一次；**不是 Runtime 重复，是 UI 策略**。

---

## 2. 目标与非目标

### 2.1 目标

1. **正文只留一份定稿**：流式预览 → 终态前擦除预览段 → Markdown + footer。  
2. **工具块可读且完整线索**：  
   - 工具名始终完整；  
   - 参数必有摘要（过长折叠截断 + 长度提示）；  
   - 结果必有展示（空也标明；过长多行折叠）。  
3. **消灭固定 2 行改写**：按块高度更新，避免双 `⏺`。  
4. **零 Runtime 改动**（除非测试发现事件缺口；默认不改）。  
5. **不上 TUI**；折叠 = 默认截断展示，非交互 expand。

### 2.2 非目标

- Textual / 全屏 TUI  
- 工具结果流式 delta  
- CLI 内点开展开全文（全文看 Session / `pickel observe`）  
- 擦除动画（V1 瞬间替换即可）

---

## 3. 设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 统一 **块模型**（StreamBlock / ToolBlock），共用行账 | 正文 settle 与工具定稿同一套「记高度 → 擦 → 重画」 |
| D2 | 终态正文：**有预览则先擦再 MD**；无预览则只 MD | 消双份；非流式路径不变 |
| D3 | 中间 step 白字：**不 settle** | react 仅无 tool 的最终 step 发 `assistant_message`；中间「让我看看」保留白字可接受 |
| D4 | 工具：**按 `tool_call_id` 的 ToolBlock**；completed 用块高度擦写整块，禁止写死 UP 2 | 根治双 label |
| D5 | 折叠 = 截断 + `… +N lines / M chars` 提示 | 无 TUI 焦点；完整内容在 Session |
| D6 | 非终端 / `record`：**不发 cursor**；工具可「completed 打完整块」；正文保证只一份 | 与 E3 测试策略一致 |
| D7 | 中断：不擦空预览；已 completed 工具保留 | 对齐 E2 中断语义 |
| D8 | 不上 Live（V1） | 与 E3 一致，降低依赖面；行账 + Control 足够 |

### 3.1 目标版式

```text
You > 今天是什么日子呢?

喵~ 让我看看今天是什么日子 🐱     ← 中间 step 流式，不 MD

⏺ shell_exec  ok  (0.2s)
  args  command='date "+%Y年%m月%d日 %A"'
  out   2026年07月27日 Monday

今天是 **2026 年 7 月 27 日，星期一** …   ← 仅定稿 MD（预览已擦）
                     anthropic / … · 2.4k→180 · 1.2s
```

长参 / 长结果：

```text
⏺ write_file  ok  (0.1s)
  args  path='src/foo.py'
        content=<1842 chars>  [折叠 · 前 3 行]
        | def main():
        |     ...
        |     return 0
        | … +42 lines
  out   wrote 1842 chars
```

---

## 4. 架构

```text
Runtime 事件（不变）
        │
        ▼
event_renderer.ChatEventRenderer   # 仍只分派
        │
        ├── StreamRenderer         # 预览段 + settle(擦→交给 message)
        ├── ToolRenderer           # ToolBlock 字典，按 id 块级更新
        └── render.message         # Markdown + footer（定稿）
        │
        └── render/lines.py（新）  # display_lines / erase_lines 共用
```

**红线继承**：runtime 不 import rich；渲染不读 Run/Session；订阅者异常隔离。

---

## 5. 模块规格

### 5.1 `render/lines.py`（新）

| API | 行为 |
|-----|------|
| `display_line_count(text: str, width: int) -> int` | 按终端宽折行 + 文本内 `\n` 计**物理行**；`width>=1` |
| `erase_lines(console, n: int) -> None` | `n>0` 且 `is_terminal`：上移 n 行并逐行 `ERASE_IN_LINE`；否则 no-op |

规则：空串 → 0 行；末尾无换行的尾行也计 1（与 rich 实际占行对齐，单测用固定 width 锁死）。

### 5.2 StreamRenderer 增强

状态仍：`idle | thinking | text`。

新增字段：

- `_buffer_text: str`（仅 text 段，供行账；thinking 可计入同一预览段或单独——**V1：thinking+text 同属当前预览段**，settle 时一并擦）
- `_preview_lines: int`（累计已打印物理行，含 `· 思考中……`）

| 方法 | 行为 |
|------|------|
| `on_thinking` / `on_text` | 现逻辑 + 按增量更新 `_preview_lines` |
| `end()` | 仅收尾换行、复位状态机；**不擦、不清「历史已落下」的中间 step**——若即将接 tool，预览段视为**已提交历史**，`_preview_lines=0` 且不再 settle 该段 |
| `settle(text, usage, fallback_label)` | 若当前预览活跃：`end` 收尾；若 `_preview_lines>0` 且 terminal：`erase_lines`；再 `render_assistant(...)`；清零预览账 |

**分段语义（关键）**

| 时机 | 动作 |
|------|------|
| `ToolCallStarted` / `StepStarted` | `stream.end()`：当前预览**落下为历史**（不擦、不 MD），行账清零 |
| `AssistantMessageEvent` | `stream.settle(...)`：只处理**当前未落下**的预览（通常是最终 step 的流式） |

中间 step 已 `end` 过的白字留在屏上；最终 step 的流式在 settle 时被擦掉换成 MD。

### 5.3 ToolRenderer 重做（块模型）

每个 `tool_call_id` 一条记录：

```text
started_at, name, arguments, height, printed_started: bool
```

**started 版式**（可多行，记 `height`）：

```text
⏺ {name}
  args  {折叠后的参数体}
  … running
```

**completed 版式**（同结构，状态替换）：

```text
⏺ {name}  ok|failed  (elapsed?)
  args  {同上}
  out   {折叠后的结果体}   # empty → out  (empty)
```

| 路径 | 行为 |
|------|------|
| terminal 且有 started 记录 | `erase_lines(height)` → 打印 completed 块 |
| 无 started / 非 terminal | 直接打印 completed 完整块（非 terminal 时 started 可只打一行 `⏺ name` 或也打块；**推荐 started 仍打块、completed 追加 out 行且不擦**，避免测试依赖 cursor——见下） |

**非终端推荐（可测）**

- started：打印头 + args + running  
- completed：**不擦**；打印 `ok/failed` + out（允许 running 残留在上——record 模式可接受）；或 completed 只追加 status+out 两行  

以测试稳定优先：非终端 **completed 追加 status 行 + out 块**，不断言「running 消失」。

**参数折叠**

| 规则 | 值 |
|------|-----|
| 优先键序 | `command`, `path`, `pattern`, `file_path`, 其余按名字排序 |
| 单 value 字符串/repr > 120 字符 | 显示 `<N chars>` + 可选前 3 行预览（前缀 `\| `） |
| `content` 键 | 默认不 dump 全文，`<N chars>` + 前 3 行 |
| 总预览行上限 | args 体最多 8 行（含键行），超出 `… +K lines` |

**结果折叠**

| 规则 | 值 |
|------|-----|
| 空 | `out  (empty)` |
| ≤5 行且 ≤400 字符 | 原样多行缩进（保留换行，不压成单行） |
| 更长 | 前 5 行 + `… +N lines / M chars` |
| `is_error` | 头行 `failed` 红色；out 同折叠 |

**耗时**：仍用 started/completed 的 `occurred_at` 差；&lt;0.1s 不显示。

### 5.4 event_renderer 分派变更

| 事件 | 新行为 |
|------|--------|
| `TextDelta` / `ThinkingDelta` | 不变 → stream |
| `ToolCallArgsDelta` | 仍不上屏 |
| `StepStarted` | `stream.end()`（落下预览，不 settle） |
| `ToolCallStarted` | `stream.end()` + `tool.on_started` |
| `ToolCallCompleted` | `tool.on_completed` |
| `AssistantMessageEvent` | **`stream.settle(...)`**（内含 render_assistant），不再单独 `end`+`render_assistant` 导致双份 |
| `TurnInterrupted` | `stream.end()`（不 settle 擦空）+ `render_interrupted` |

### 5.5 message.py

`render_assistant` 保持；由 settle 调用。无逻辑分叉也可不改。

---

## 6. 文件清单

| 路径 | 动作 |
|------|------|
| `src/pickel/cli/render/lines.py` | 新建 |
| `src/pickel/cli/render/stream.py` | 改：行账 + `settle` |
| `src/pickel/cli/render/tool.py` | 改：块模型 + 折叠 |
| `src/pickel/cli/event_renderer.py` | 改：分派接 settle |
| `src/pickel/cli/render/message.py` | 原则上不动 |
| `tests/cli/test_render_lines.py` | 新建 |
| `tests/cli/test_render_stream.py` | 改 / 增 settle |
| `tests/cli/test_streaming_render.py` | 改：正文只一份 |
| `tests/cli/test_event_rendering.py` 等 | 按新版式更新断言 |
| `tests/cli/test_tool_render_blocks.py` | 新建：块与折叠 |

**禁止**：改 `runs/strategy/react.py`、事件类型定义（除非发现硬缺口）。

---

## 7. 实施任务

> 测试命令：`uv run --with pytest python -m pytest <path> -q`（仓库根）  
> TDD：红 → 绿 → commit  

### Task 1: 行账工具 `render/lines.py`

- [x] 失败测试：固定 width 下多行/折行/空串的 `display_line_count`  
- [x] 实现 `display_line_count` + `erase_lines`（终端直写 ANSI）  
- [x] 通过

### Task 2: ToolBlock 块模型 + 折叠

- [x] 失败测试（名/参/结果/折叠/非终端）  
- [x] 实现 `tool.py`  
- [x] 通过

### Task 3: Stream settle

- [x] 失败测试：settle 后正文一份 + footer  
- [x] `end` 后预览不参与 settle  
- [x] 实现 stream.settle + event_renderer  
- [x] 通过

### Task 4: 回归与版式对齐

- [x] `tests/cli/ -q` 全绿（124 passed）  
- [x] 更新 chat_loop 集成断言

### Task 5: 真机抽检（人工）

```bash
PICKEL_TRACE=1 uv run pickel
# 1）无工具短答：正文一份 + footer
# 2）shell_exec 短命令：单 ⏺、args、out 可见
# 3）写长文件/长输出：折叠提示存在
```

---

## 8. 测试口径摘要

| 用例 | 断言 |
|------|------|
| 流式+定稿 | 最终可见一份正文语义；footer 有；无「同文两套列表符号连打」 |
| 仅定稿无 delta | 与现非流式一致：MD + footer |
| 工具短 | `⏺ name` 一次；`args`；`ok`/`out` |
| 工具长参 | 名在；`<N chars>` 或折叠行 |
| 工具长结果 | 多行前缀 + `… +N` |
| 中断 | 含「已中断本轮」；不要求擦预览 |
| 非终端 | 不依赖 cursor；关键字段可 grep |

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 行账与 rich 实际折行差 1 | 固定 width 单测 + 真机；偏差时 settle 宁可多擦 0 行历史（中间 step 已 end 清零） |
| 流式中改终端宽度 | 文档约定；不处理 resize |
| 超长预览上移数百行 | V1 可设阈值：预览行 &gt; 80 则不擦、只打 MD（并 dim 一行「预览未清除」）——可选，Task 3 后视真机 |
| 旧测试绑死双渲/180 字 | Task 4 统一改口径 |

---

## 10. 与后续 TUI 的关系

本方案把 UI 收成 **「事件 → 块状态 → 绘制」**。将来 TUI 作为第二个 EventBus 订阅者：

- 复用同一事件；  
- 折叠改为 widget 本地状态；  
- settle = 替换预览节点为 MD 节点（比 ANSI 更简单）。

**不在本期实现 TUI。**

---

## 11. 验收标准

1. 有工具的一轮对话：最终用户可见回复 **不重复**。  
2. 每个工具调用：屏上 **一个** 头行名；**可见 args 摘要与 out**（或 empty）。  
3. `tests/cli` 绿；人工三条抽检通过。  
4. `git grep`：runtime 仍不 import rich；无新增对 Session 的渲染读取。
