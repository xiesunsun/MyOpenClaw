# Runtime 事件系统与可独立 CLI UI —— 设计稿

日期：2026-07-26
分支：`feat/runtime-events-and-cli-ui`
状态：设计已确认，待出实施计划

---

## 1. 问题

当前 CLI 的体验缺陷不是排版问题，是**事件表达力不足**。事件说不清正在发生什么，UI 就只能画得粗。

### 1.1 事件没有信封

`src/pickel/runs/events.py:18-29` 的 `RuntimeEvent` 没有 `event_id` / `session_id` / `turn_id` / `occurred_at` / 序号。

后果：事件一旦离开当前进程就无法排序、去重、回放。「跳回上一个 turn」「回放整场会话」「把 runtime 跑在别处、UI 在本地渲染」全都无从谈起。

讽刺的是**信封所需的一切都已经在运行态里**：`runs/turn_state.py:25` 有 `turn_id`，`turn_state.py:16` 有 `step_index`，`react.py` 每次发事件时这些值都在手边——只是没往事件里放。

### 1.2 provider 没有暴露增量

`src/pickel/providers/base.py:20` 只有 `generate()`。

`anthropic.py:62` 实际**已经在用** `client.messages.stream()`，但 `anthropic.py:65` 立刻 `get_final_message()` 把增量吃掉了。传输层是通的，delta 被丢弃了。gemini 侧 (`gemini.py:65`) 用的是非流式 `generate_content`。

后果：没有 token 级事件，UI 只能等一整段回复。打字机、实时思考、边跑边显示的工具输出，一个都做不了。这是体感的根因——按回车之后是一片死寂，然后突然一大段。

### 1.3 两套并行的事件体系

| | `hooks/events.py` | `runs/events.py` |
|---|---|---|
| 信封 | 完备（`event_id`/`session_id`/`turn_id`/`step_index`/`occurred_at`） | 无 |
| 结构 | 每个时机一个类型 | 一个扁平 dataclass 塞 8 个可选字段 |
| 语义 | 同步、有返回值、可改写可阻断 | fire-and-forget |

`PreToolUseEvent` 与 `TOOL_CALL_STARTED` 是同一时刻的两个不同对象，字段不通用，各写各的。

### 1.4 UI 层自身的耦合

- `chat.py:179` 的 `_render_assistant_message` 与 `event_renderer.py:52` 的 ASSISTANT_MESSAGE 分支渲染同一件事，逻辑重复两份
- 靠 `chat.py:592` 的 `rendered_assistant_message` 标志位互相躲闪
- `chat.py` 593 行里混着四件事：输入循环、命令解析、渲染、session/模型管理
- 错误处理是 `chat.py:587` 的 `traceback.format_exc()` 直接糊到屏幕上

runtime 侧的解耦其实已经基本做到了——`react.py` 全程只调 `_emit_event`，从不碰 Console。真正欠缺的是事件的表达力，不是调用方向。

---

## 2. 范围与分解

三个子系统，顺序交付，每个独立可测：

```
E1 事件底座 ──> E2 streaming + 中断 ──> E3 UI 重构
```

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **E1** | 信封、tagged union 事件、EventBus、JSONL sink | — |
| **E2** | `provider.stream()`、delta 事件、中断语义 | E1 的信封 |
| **E3** | 拆 `chat.py`、新渲染层、无边框排版 | E2 的 delta 事件 |

E1 完成后 UI 行为不变（旧 renderer 适配新事件），是纯底座替换。

---

## 3. 事件信封

```python
@dataclass(frozen=True)
class EventEnvelope:
    event_id: str            # uuid4
    session_id: str
    turn_id: str
    seq: int                 # 单调递增，session 内全序
    occurred_at: datetime
    step_index: int | None = None
```

`hooks/events.py:19` 的 `HookEventBase` 已有除 `seq` 外的全部字段，改为继承 `EventEnvelope` 即可，hook 侧改动极小。

**`seq` 由 EventBus 统一分配**，不由发射点自己维护。这是全序的唯一来源，也是回放能重建时序的前提。

### 3.1 为什么不合并 hook 与 runtime 事件

两者共享信封，但**语义不合并**：

| | hook 事件 | runtime 事件 |
|---|---|---|
| 调用 | 同步阻塞，等返回值 | fire-and-forget |
| 能力 | 可改写 ModelContext、可阻断工具 | 只读通知 |
| 失败 | 应当中止或降级 | 不得影响 runtime |

合并会让 UI 订阅者意外获得改写 agent 行为的能力——一个渲染器不该能阻断工具调用。

---

## 4. 事件清单

`RuntimeEvent` 从扁平 dataclass 改为 tagged union。现状是一个类塞 8 个可选字段，`tool_call` 与 `text` 永远只有一个有意义，类型系统帮不上任何忙。

```python
@dataclass(frozen=True)
class RuntimeEventBase:
    envelope: EventEnvelope
    def to_dict(self) -> dict: ...
```

| 事件 | 载荷 | 阶段 |
|---|---|---|
| `turn_started` | `user_text` | E1 |
| `step_started` | `step_index` | E1 |
| `thinking_delta` | `text` | E2 |
| `text_delta` | `text` | E2 |
| `tool_call_started` | `tool_call`, `call_index`, `total_calls`, `batch_id` | E1 |
| `tool_call_delta` | `tool_call_id`, `partial_arguments` | E2 |
| `tool_call_completed` | `tool_call`, `tool_result` | E1 |
| `assistant_message` | `text`, `usage`, `metadata` | E1 |
| `turn_completed` | `TurnUsage`, `elapsed_ms` | E1 |
| `turn_failed` | `error_type`, `message`, `traceback` | E1 |
| `turn_interrupted` | `at_step`, `partial_text` | E2 |

`tool_call_failed` 并入 `tool_call_completed`——失败信息已在 `ToolExecutionResult.is_error` 里，两个事件类型表达同一件事是冗余（现状 `events.py:13-14`）。

### 4.1 与可观测性 O1 的衔接

`turn_completed` 携带 `runs/turn_usage.py` 的 `TurnUsage`，UI 的 footer 直接用它，不再自行拼装 `MessageMetadata`。这消掉 `chat.py:209` 与 `event_renderer.py:113` 两份重复的 footer 逻辑。

`/context` 的 measure 是**拉取式**（用户主动问才算），事件是**推送式**，两条路径互不干扰，共用 `TurnUsage` 这一个口径。

---

## 5. EventBus

```python
class EventBus:
    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]
    async def emit(self, event: RuntimeEventBase) -> None
```

现状是把单个 `event_handler` 一路当参数传下去（`run.py:142` → `react.py:53`），只能有一个订阅者。

**订阅者异常必须隔离**：UI 渲染出错不该杀掉正在跑的 turn。bus 捕获每个 handler 的异常，记录后继续分发给其余订阅者。

`seq` 在 `emit` 内分配，保证同一 session 的全序。

handler 沿用现状签名 `Callable[[RuntimeEventBase], Awaitable[None] | None]`——同步与异步 handler 都允许，bus 负责判别。JSONL sink 是同步的，强制异步只会到处加无谓的 `async`。

---

## 6. streaming 合同

```python
# providers/base.py
async def generate(self, ctx: ModelContext) -> AssistantMessage: ...
async def stream(self, ctx: ModelContext) -> AsyncIterator[StreamDelta]: ...
```

**`generate()` 必须用 `stream()` 实现**：

```python
async def generate(self, ctx):
    return await accumulate(self.stream(ctx))
```

两条独立代码路径必然漂移——非流式路径会悄悄少处理一种 block 类型，然后只在某个模型上出问题。单一实现是唯一能长期维持一致的形态。

### 6.1 StreamDelta

provider 层原语，由 `react` 翻译成 RuntimeEvent：

```python
StreamDelta = TextDelta | ThinkingDelta | ToolCallDelta | UsageFinal
```

### 6.2 thinking 块的 signature

Anthropic 的 thinking 块带 `signature`，下一轮回传时必须原样附上（`anthropic.py:176-177` 已在处理）。增量组装必须完整保留它——丢了会导致下一轮请求被 provider 拒绝。这是 E2 最容易出错的地方，需要专门的测试覆盖。

### 6.3 gemini

换用 `aio.models.generate_content_stream`。gemini 的 thinking 与 tool_call 增量结构与 anthropic 不同，两个 provider 各自负责翻译到统一的 `StreamDelta`。

---

## 7. 中断语义

用 asyncio 原生取消：UI 捕 Ctrl-C → cancel 当前 turn 的 task → `react` 捕 `CancelledError`。

**落盘规则**（这是正确性问题，不是体验问题）：

- 已完成的 step 正常落盘
- 进行中的 assistant 消息按「被中断」标记落盘
- **已发出但未完成的 tool_call 必须补一条标记为中断的 tool_result**

最后一条是硬性的：session 里若留下一条没有对应 `tool_result` 的 `tool_call`，下一轮请求会被 provider 直接拒绝。中断不能把会话弄成不可继续的状态。

中断后 emit `turn_interrupted`，UI 显示已生成的部分而非丢弃。

---

## 8. 事件落盘与回放

**默认关闭。** 工具参数与文件内容会进入 trace，默认开启是隐私问题。

- 位置：`~/.pickel/traces/<session_id>.jsonl`
- 开启：`settings.json` 的 `trace_enabled`（默认 `false`），环境变量 `PICKEL_TRACE=1` 覆盖之。扁平键名与既有的 `react_max_steps` / `context_cli_turn_window` 风格一致，走 `loader.py` 的 `_BUILTIN_DEFAULTS` + `AppConfig` 字段这条既有路径
- 格式：一行一个事件的 `to_dict()`

### 8.1 与「真源唯一」红线的关系

2026-07-26 可观测性设计的第一条红线是「Session entry + `metadata.usage` 为唯一真源，禁止平行 telemetry 库」。JSONL trace 与这条红线的边界必须写死：

- trace **不是**对话事实的真源，是派生的可观测轨迹
- **禁止任何代码从 trace 读回来重建对话或用量**——回放只用于人看和调试
- trace 可随时删除、可全程关闭，删了不影响 agent 任何行为
- 若 trace 与 Session 冲突，以 Session 为准

违反这几条就退化成了平行事实库。

---

## 9. UI 层结构

```
cli/
  chat.py          593行 → 只剩 loop：读输入、分派、等待
  commands/        每个 slash command 一个函数 + 注册表
  render/
    stream.py      订阅 delta，Live 区域出字
    tool.py        工具行原地 running → ok
    message.py     assistant / system / error
    context.py     已有的 ContextRenderer（不动）
```

### 9.1 排版

无边框、符号前缀、靠缩进分层。去掉所有 Panel：

```
> 帮我看下 test_measure 为什么挂了

· 思考中……
  先看测试本体，再看 measure 的归一化分支。

  ⏺ read_file  tests/runs/test_measure.py
    194 行
  ⏺ shell_exec  pytest tests/runs/test_measure.py -q
    1 failed, 10 passed  (2.3s)

失败在 `test_分栏之和恒等于_total`。原因是夹上限时
`assigned` 没随循环累加。

                     anthropic/claude-jupiter-v1-p · 2.4k→180 · 3.1s
```

工具行**原地更新**（running → ok），不再刷两遍。这需要 `tool_call_started` 与 `tool_call_completed` 能通过 `tool_call_id` 配对——信封让这件事变得可靠。

### 9.2 消除重复

- 删掉 `chat.py:179` 的 `_render_assistant_message` 与 `chat.py:209` 的 `_render_assistant_footer`（与 `event_renderer.py` 重复）
- 删掉 `chat.py:592` 的 `rendered_assistant_message` 标志位
- 删掉 `chat.py:206` 的空方法 `_render_tool_batch`

渲染只有一个入口：订阅事件。

---

## 10. 测试策略

| 层 | 测法 |
|---|---|
| 信封 | `seq` 严格递增；`to_dict()` 可 JSON 序列化并往返 |
| EventBus | 一个订阅者抛异常，其余订阅者仍收到事件，turn 不受影响 |
| streaming | fake provider 吐固定 delta 序列，断言 `accumulate(stream())` 与 `generate()` 逐字段相等 |
| thinking signature | 增量组装后 signature 完整保留，可作为下一轮输入 |
| 中断 | cancel 后 session 的 active_path 上不存在缺 `tool_result` 的 `tool_call` |
| 渲染 | `Console(record=True)` 捕获输出做文本断言（沿用 `test_context_renderer.py` 的做法） |
| trace | 关闭时不产生文件；开启时事件数与 emit 数一致 |

TDD：每项先写红灯。

---

## 11. 红线

1. runtime 不得 import `rich` 或任何 UI 库——UI 依赖 runtime，反向为零
2. 订阅者异常不得影响 turn 执行
3. `generate()` 必须由 `stream()` 实现，不得存在第二条独立路径
4. `seq` 只由 EventBus 分配，发射点不得自行编号
5. trace 是派生物，禁止任何代码从中读回重建对话或用量
6. trace 默认关闭
7. 中断后 session 必须处于可继续状态（无悬空 tool_call）
8. hook 事件与 runtime 事件共享信封，但不共享语义——runtime 事件订阅者不得具备改写能力
9. thinking 块的 signature 在增量组装中必须完整保留

---

## 12. 不做什么

- **跨进程协议**：本轮是进程内解耦。事件设计成「可外送的形状」，但不引入 stdio/JSON-RPC 传输层、不做协议版本协商。将来需要时加传输层，事件本身不用改。
- **工具审批 UI**：`hooks/decisions.py` 的 `PreToolUse` 已具备阻断能力，审批的 runtime 基础在。但 UI 侧的交互（弹窗、记住选择、权限模式）是独立议题。
- **底部固定状态栏**：rich `Live` 与 prompt-toolkit 输入共存有真实难度，本轮不做。footer 跟在每条回复后面。
- **prompt cache**：`cache_read`/`cache_write` 在 pickel 内恒为 0（provider 从不发 `cache_control`）。启用涉及断点放置策略，是独立议题。
