# E3 无边框 CLI 渲染实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CLI 渲染从 Panel 框式改为无边框排版（符号前缀 + 缩进分层），工具行原地 running → ok，渲染收敛到唯一入口（事件订阅），删除 chat.py 的双渲染路径。

**Architecture:** `event_renderer.py` 变成纯分派器，实际渲染拆进 `cli/render/`（stream / tool / message 三模块）。所有渲染信息只来自事件——UI 不摸 Run/Session。工具行配对靠 `tool_call_id`，耗时靠信封 `occurred_at` 差值，零 runtime 改动。

**Tech Stack:** rich（Text/Markdown/Console，不用 Live、不用 Panel）、既有 EventBus 事件流。

**基分支：** `feature/context-request-prepare-design`（b20e02f 起）。工作分支 `feat/e3-borderless-ui`。

## Global Constraints

- 全仓测试基线（本分支起点）：`GEMINI_API_KEY=fake uv run --with pytest pytest -q`——以 Task 1 开始前实测为准记入 ledger；此后 failed 集合不得增减。
- 测试命令统一：`GEMINI_API_KEY=fake uv run --with pytest pytest <路径> -q`，仓库根执行；每次 commit 前 `git checkout uv.lock`。
- 红线（设计稿 §11）：runtime 不得 import rich；渲染只订阅事件，不得读 Run/Session/trace；`seq` 不由 UI 编号。
- footer 的输入规模一律用 `usage.actual_input_tokens`（O1 §5.1 口径：input + cache_read + cache_write），禁止退回裸 `input_tokens`。
- `context_renderer.py` 不动；`chat.py` 只碰渲染相关段与 `_loop` 末尾的 fallback——装配段（`from_boot`、`_handle_reload_command`、extension/MCP 相关）是另一团队 S2 主战场，一行不碰。
- 中断语义与 E2 保持：`已中断本轮` 提示仍由 `TurnInterrupted` 事件驱动渲染。

## 关键设计决策

1. **不用 rich Live**（设计稿 §12）：工具行原地更新用 ANSI 序列——started 打两行（label 行 + `running…` 行），completed 时若 console 是终端（`console.is_terminal`）则光标上移两行、逐行清除重写；非终端（测试 record、管道）降级为直接追加结果行，不重写。测试两种模式都要覆盖。
2. **工具耗时不加 runtime 字段**：`ToolCallStarted`/`ToolCallCompleted` 的信封 `occurred_at` 都是 datetime，UI 侧按 `tool_call_id` 配对相减。配不上（找不到 started）就不显示耗时。
3. **E2 遗留 footer 修复在 UI 侧**：`AssistantMessageEvent.usage=None` 时事件里没有 model 信息；给 `ChatEventRenderer` 构造注入 `fallback_model_label`（ChatLoop 每轮 `create_event_bus` 时从自身配置取），usage=None 时 footer 只显示这个 label。不改 runtime 事件结构。
4. **`commands/` 拆分暂缓**：chat.py 拆 slash-command 注册表与对方 S2（extension 宿主）正面冲突，等 S2 合并后单独做。本计划完成后 chat.py 仍持有 `_handle_*_command` 方法。
5. **排版规范**（设计稿 §9.1 样例的落地口径）：
   - thinking 增量：首个 delta 前打一行 `· 思考中……`（dim），增量文本 dim 输出；thinking → text 切换时补换行。
   - text 增量：直接输出（无缩进——最终 `AssistantMessageEvent` 到达后正文会以 Markdown 重渲，流式只是预览，与 E2 相同）。
   - 工具行：`⏺ name  args摘要`（单行截断到 console 宽度内），结果行缩进两格 `  ok · 结果摘要 (2.3s)` 或 `  failed · …`。
   - assistant 正文：Markdown 无框；footer 单行右对齐 dim：`{model_label} · {actual_input}→{output} · {elapsed}s`，token 数 ≥1000 时以 `k` 缩写（`2.4k`）。
   - system `· {text}`（cyan），error `✗ {text}`（red），中断 `✗ 已中断本轮。`（yellow）——「已中断本轮」字样保留（真机 pexpect 脚本按它断言）。
   - header：无框三行（agent、config 路径、命令列表），样式沿用现配色。
6. **文件结构**：

```
src/pickel/cli/
  render/
    __init__.py
    message.py   # header / system / error / assistant正文+footer
    stream.py    # StreamRenderer：thinking/text 增量状态机
    tool.py      # ToolRenderer：⏺ 行、原地更新、耗时配对
  event_renderer.py  # ChatEventRenderer 保留类名与 handle_event 接口，变分派器
  chat.py            # 删双渲染路径，header/system/error 换 render.message
```

---

## Task 1: render/message.py——无边框静态消息渲染

**Files:**
- Create: `src/pickel/cli/render/__init__.py`（空）
- Create: `src/pickel/cli/render/message.py`
- Test: `tests/cli/test_render_message.py`

**Interfaces:**
- Produces:
  - `render_header(console, *, agent_id: str, commands_line: str) -> None`
  - `render_system(console, text: str, *, style: str = "cyan") -> None`（`· ` 前缀）
  - `render_error(console, text: str) -> None`（`✗ ` 前缀，red）
  - `render_interrupted(console) -> None`（`✗ 已中断本轮。`，yellow）
  - `render_assistant(console, *, text: str, usage: TurnUsage | None, fallback_model_label: str | None) -> None`
  - `format_footer(usage: TurnUsage | None, fallback_model_label: str | None) -> str | None`（纯函数便于测试；两者皆空返回 None）
  - `abbrev_tokens(n: int) -> str`（`180` → `"180"`，`2437` → `"2.4k"`）

**行为要点：**
- `render_assistant`：正文 `Markdown(text)` 直接 print（无 Panel）；footer 为 None 时不打 footer；footer 行 `Text(footer, style="dim", justify="right")`。
- `format_footer`：usage 非空 → `f"{label} · {abbrev(actual_input)}→{abbrev(output)} · {elapsed/1000:.1f}s"`（label 取 `usage.model_label`，为空退 fallback；elapsed_ms 为 0 时省略时间段）；usage 为 None 且 fallback 非空 → 只有 label。**输入规模必须用 `usage.actual_input_tokens`。**
- 测试用 `Console(width=100, record=True, force_terminal=False)` + `export_text()` 断言；必须含：`✗`/`·` 前缀存在、输出不含 `╭`（无 Panel）、`format_footer` 的 §5.1 口径测试（cache_read=8000, input=100 → `8.3k`——cache 不计入会得 `100`，测试要能杀死这个变异）、usage=None + fallback 时 footer 只有 label、`abbrev_tokens` 边界（999/1000/2437）。

**Steps:** 失败测试 → 实现 → 通过 → commit `feat(cli): render/message 无边框消息渲染`。

---

## Task 2: render/stream.py——流式增量状态机

**Files:**
- Create: `src/pickel/cli/render/stream.py`
- Test: `tests/cli/test_render_stream.py`

**Interfaces:**
- Produces: `class StreamRenderer:`
  - `__init__(console)`
  - `on_thinking(text: str) -> None`
  - `on_text(text: str) -> None`
  - `end() -> None`（活跃时补换行并复位；幂等）
  - `property active: bool`

**行为要点（状态机 idle/thinking/text）：**
- idle→thinking：先打 `· 思考中……`（dim）+ 换行，随后增量 dim 输出；thinking→text：补换行再输出正常文本；idle→text：直接输出。
- 所有增量 `console.print(..., end="", highlight=False, markup=False)`（防增量文本被 rich 解析——沿用 E2 决策）。
- `end()`：非 idle 时 print 换行、置 idle；idle 时无输出（幂等，测试断言调两次只出一个换行）。

**测试必须含：** 三种状态迁移的输出顺序断言；`markup=False` 守护（增量含 `[red]` 字面输出）；`end()` 幂等。

**Steps:** 失败测试 → 实现 → 通过 → commit `feat(cli): render/stream 流式增量状态机`。

---

## Task 3: render/tool.py——工具行原地更新

**Files:**
- Create: `src/pickel/cli/render/tool.py`
- Test: `tests/cli/test_render_tool.py`

**Interfaces:**
- Consumes: `ToolCall`（name/arguments/id）、`ToolExecutionResult`、信封 `occurred_at`
- Produces: `class ToolRenderer:`
  - `__init__(console)`
  - `on_started(tool_call, occurred_at) -> None`
  - `on_completed(tool_call, tool_result, occurred_at) -> None`

**行为要点：**
- `on_started`：打 `⏺ {name}  {args摘要}`（args 摘要沿用现 `_format_tool_label` 的截断规则，整行再截到 console 宽度内保证单行）+ 缩进行 `  running…`（dim）。按 `tool_call.id` 记 `{id: (occurred_at, label_lines)}`。
- `on_completed`：状态行文本 `  ok · {结果摘要}` / `  failed · {结果摘要}`（摘要沿用现 `_truncate_content`）；配对到 started 且间隔 ≥100ms 时追加 ` ({secs:.1f}s)`。
  - `console.is_terminal` 为真：ANSI 上移两行清除重写（`console.control` / 直接写 `\x1b[2A\x1b[0J` 后重打 label 行 + 状态行）。
  - 非终端：不重写，直接追加状态行（record 模式输出里 running 行与结果行都在——测试按此断言）。
- 配不上 started（乱序/丢失）：直接打完整两行，不炸。

**测试必须含：** 非终端模式 running 行与 ok 行都出现且顺序正确；failed 样式分支；耗时显示（构造两个相差 2.3s 的 occurred_at，断言 `(2.3s)`）；配不上 started 不炸；终端模式（`force_terminal=True` 的 record Console）输出含 ANSI 控制序列且最终文本含结果行。

**Steps:** 失败测试 → 实现 → 通过 → commit `feat(cli): render/tool 工具行原地更新`。

---

## Task 4: event_renderer.py 改分派器 + 既有测试口径更新

**Files:**
- Modify: `src/pickel/cli/event_renderer.py`（重写为分派器）
- Modify: `tests/cli/test_streaming_render.py`、`tests/cli/test_event_renderer*.py`（若存在）等既有断言
- Test: 既有文件内更新

**Interfaces:**
- Produces: `ChatEventRenderer.__init__(console, *, fallback_model_label: str | None = None)`；`handle_event` 签名不变；`rendered_assistant_message` 属性**保留**（Task 6 才删 chat.py 的 fallback，本任务不能先斩接口）。

**分派表：**
- `TextDeltaEvent` → `stream.on_text`；`ThinkingDeltaEvent` → `stream.on_thinking`
- `ToolCallArgsDeltaEvent` → 忽略（维持 E2 决策）
- `StepStarted` → `stream.end()`（不再打 `Step N` 行——无边框排版下是噪音；对应旧断言删除）
- `ToolCallStarted` → `stream.end()` + `tool.on_started(event.tool_call, event.envelope.occurred_at)`
- `ToolCallCompleted` → `tool.on_completed(...)`
- `TurnInterrupted` → `stream.end()` + `render_interrupted(console)`
- `AssistantMessageEvent` → `stream.end()` + `render_assistant(console, text=event.text, usage=event.usage, fallback_model_label=self._fallback_model_label)` + 置 `rendered_assistant_message = True`

**既有测试口径更新原则：** 行为断言换新排版口径（`已中断本轮` 字样保留；「流式文字行不含框字符」的行级守护改为「流式文字行不含 `⏺` 且 assistant 正文另起行」——保住 `_end_streaming` 等价守护，即 Task 7 fix round 加的三条参数化测试必须有等价替身，不得静默删除）。Panel 相关断言（`╭`、`Assistant` 标题）删除，新增「输出不含 `╭`」反断言。

**Steps:** 先改测试跑红 → 重写 event_renderer → 全 cli 测试绿 → commit `refactor(cli): event_renderer 改分派器，无边框排版`。

---

## Task 5: chat.py 消除双渲染路径

**Files:**
- Modify: `src/pickel/cli/chat.py`
- Modify: `tests/cli/test_chat_loop.py`

**改动清单（只删/只换，不动装配段）：**
1. 删 `_render_assistant_message`、`_render_assistant_footer`、`_render_tool_batch`、`render_turn_output`（其唯一测试 `test_render_turn_output_replays_assistant_tool_batch` 一并删）。
2. 删 `_loop` 末尾 fallback：`if not event_renderer.rendered_assistant_message: self._render_assistant_message(reply)` 两行；随后从 `event_renderer.py` 删掉 `rendered_assistant_message` 属性（全仓 grep 确认零引用后）。
3. `_render_header`/`_render_system_message`/`_render_error_message`/`_render_message` 改为调 `render.message` 的对应函数；`_render_message`（Panel 通用方法）删除，调用点逐个换。
4. `create_event_bus` 给 `ChatEventRenderer` 传 `fallback_model_label`（从当前 run/agent 配置组 `"{provider} / {model}"`，与 TurnUsage.model_label 同格式；取法参照 `_handle_model_command` 里现成的配置读取路径）。
5. `MessageMetadata` 若仅剩 `_render_assistant_*` 使用，其 import 与定义处置一并清理（先 grep 确认）。

**验收：** `grep -n "Panel" src/pickel/cli/chat.py src/pickel/cli/event_renderer.py` 零命中（`context_renderer.py` 与 `render/` 不在此列——render/ 本就不该用 Panel，context_renderer 不动）；`grep -rn "rendered_assistant_message" src/ tests/` 零命中；渲染入口唯一（事件订阅）。

**Steps:** 先改测试跑红 → 改 chat.py → 全仓绿 → commit `refactor(cli): 渲染唯一入口，删双路径`。

---

## Task 6: 组装层集成测试

**Files:**
- Test: `tests/cli/test_chat_loop.py`（追加）

**内容：** 真 `Run` + 流式 fake provider（thinking×1 + text×3 + 带 usage 的 StreamCompleted）+ 一次工具调用（echo 类立即返回），跑一整轮，`export_text()` 断言：
- 输出全程不含 `╭`（无 Panel 残留）
- `· 思考中……` 在流式文字前
- `⏺ echo` 行存在，结果行含 `ok`
- footer 含 model label 与 `→`（in→out 格式）
- 顺序：thinking 行 < 流式文字 < 工具行 < 正文 Markdown

有牙验证：临时把 fake provider 的 usage 置 None，footer 断言换 fallback label 路径也要一条测试。

**Steps:** 失败测试 → （应直接绿，若红则修）→ commit `test(cli): 无边框渲染组装层集成测试`。

---

## Task 7: 真机与交互视觉验证（scratchpad，不进仓库）

1. 复用 E2 的 `e2_interrupt_tui.py` 模式写 `e3_visual.py`：pexpect 跑 `uv run pickel chat`，一轮带工具的问题 + 一轮纯文本中断（Ctrl-C），断言：输出无 `╭`、`⏺` 出现、`已中断本轮` 出现、中断后继续对话成功、正常退出。加载 key：`set -a && . ~/.pickel/.env && set +a`，**绝不打印任何环境变量值**；真实调用失败最多重试 2 次。
2. 终端原地更新人工确认项（pexpect 抓 ANSI 难断言重写效果）：把最终屏幕文本里 `running…` 不应残留作为断言（tty 下被重写清除）。
3. 全仓最终回归 + 验收清单核对。

**验收清单：**
- [ ] 全仓：基线 failed 集合不增不减，新增全绿
- [ ] `grep -rn "rich" src/pickel/runs/ src/pickel/providers/` 无输出
- [ ] `grep -n "Panel" src/pickel/cli/chat.py src/pickel/cli/event_renderer.py src/pickel/cli/render/` 无输出
- [ ] `grep -rn "rendered_assistant_message" src/ tests/` 无输出
- [ ] 真机：`⏺` 工具行 + 无框正文 + footer 三段式可见
- [ ] tty 下工具行原地更新（最终屏无 `running…` 残留）
- [ ] 纯文本中断显示 `已中断本轮`，chat 不退出，可继续对话
- [ ] trace 默认关：交互后 `~/.pickel/traces/` 无新文件

**Steps:** 脚本 → 跑 → 清单核对 → commit（若有测试修补）`test(cli): E3 验收收尾`。

---

## 不做什么（本计划边界）

- `commands/` 拆分与 chat.py 瘦身到「只剩 loop」——等 S2 合并后单独立项
- 底部固定状态栏、rich Live、工具审批 UI（设计稿 §12）
- `ToolCallArgsDeltaEvent` 上屏（增量 JSON 容错解析，留给后续）
- 跨进程协议
