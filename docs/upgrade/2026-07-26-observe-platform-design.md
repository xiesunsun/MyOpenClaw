# 观测平台（pickel observe）设计

**状态**：设计稿（自主模式：按 goal 指令自评审后直接进入实施，实施后可回溯审阅）
**分支**：`feat/observe-platform`（自 `feature/context-request-prepare-design` 拉出）
**范围**：从一个 query 进入，导出该会话的完整执行轨迹（上下文变化、工具执行、时间、token、缓存）为自包含 HTML 观测平台
**不在范围**：常驻 server、实时推送、分布式 tracing、跨机聚合、写回任何数据

**关联**

| 文档 / 代码 | 关系 |
|------|------|
| `2026-07-26-observability-design.md` | 真源划分与红线（§3、§11），本文全部继承 |
| `2026-07-26-runtime-events-and-cli-ui-design.md` | 事件类型与 trace sink 合同 |
| blog.sunxie.me《Agent 不是模型加工具，而是一类新的系统工程对象》 | 方法目标 |
| `runs/turn_usage.py` | usage 合计派生，直接复用 |
| `runs/trace_sink.py` | trace JSONL 位置与红线 5/6 |
| `persistence/sqlite_session_repository.py` | Session 读取入口 |

---

## 1. 目标（博客设想 → 功能映射）

博客的方法目标：**「当 Agent 的表现发生变化时，我们能不能知道究竟是什么造成的？」** 完整执行链——用户输入 → Context 来源 → 发送给模型的消息 → 模型决策 → 工具调用 → 工具返回 → 状态变化——应当可审查、可复现。

| 博客要求 | 平台功能 |
|----------|----------|
| 完整执行链可审查 | 每 turn 时间线：query → step（thinking / text / tool_call）→ tool_result → 回复 |
| Context 构造可见 | 每 step 实际输入规模（input + cache_read + cache_write）曲线、compaction 标记、hook_injected_chars |
| 成本与延迟 | per-step / per-turn / per-session 的 token 四分项、elapsed_ms |
| 缓存 | cache_read / cache_write 分项与命中率 |
| 可辨识性 | 模型、finish_reason、context_fingerprint 变化点、错误（is_error 的 tool 结果）逐 step 可见 |

## 2. 数据真源与红线（继承 observability-design）

1. **主真源 = Session**（`sessions` + `session_entries`）。对话正文、tool 调用与结果、usage、elapsed_ms、provider/model 一律从 Session 读。
2. **trace JSONL（`~/.pickel/traces/{session_id}.jsonl`）= 可选时序增强，非真源。** 红线 5 禁止从 trace 重建**对话或用量**；本平台只从 trace 取 Session 里没有的**时间戳与终态事件**（turn/step/tool 的 occurred_at、turn_failed 的 traceback、turn_interrupted），逐字段白名单读取，UI 标注「trace 增强 · 非真源」。trace 缺失时平台完整可用，仅无精确时序。
3. **只读派生，可丢可重算。** 平台不写 Session、不写 trace、不发网络请求。
4. **不耦合**：runtime 不 import 观测模块；观测模块只 import `conversations` / `persistence` / `config.paths` 与 stdlib。删除整个观测模块不影响任何现有测试。

## 3. 架构

```text
Session（SQLite，真源）      traces/*.jsonl（可选增强）
        │                          │
        ▼                          ▼
  observe/collector.py      observe/trace_reader.py
        │   （只读派生）           │（白名单字段）
        └────────┬─────────────────┘
                 ▼
        TrajectoryModel（值对象，可 JSON 化）
                 ▼
        observe/html_report.py（自包含单文件 HTML）
                 ▼
        CLI：pickel observe [--session ID] [--out PATH]
```

| 单元 | 职责 | 依赖 |
|------|------|------|
| `observe/model.py` | `SessionTrajectory / Turn / Step / ToolExecution` frozen dataclass + `to_dict` | 无（纯值对象） |
| `observe/collector.py` | Session → TrajectoryModel；turn 切分、tool 配对、usage 合计（复用 `turn_usage._accumulate` 的口径） | conversations、persistence |
| `observe/trace_reader.py` | trace JSONL → 时序增强字典（entry 级白名单） | config.paths |
| `observe/html_report.py` | TrajectoryModel 列表 → 单文件 HTML（内嵌 JSON + inline CSS/JS/SVG，无外部资源） | model |
| `cli/main.py` | `observe` 子命令 | observe/* |

## 4. 轨迹模型

```python
@dataclass(frozen=True)
class ToolExecution:
    tool_call_id: str; name: str; arguments: dict
    result_preview: str          # 截断（默认 2000 字符），is_error 原样
    is_error: bool
    started_at: str | None; completed_at: str | None; duration_ms: int | None  # trace 增强

@dataclass(frozen=True)
class Step:                      # 一次 generate = 一条 assistant entry
    index: int
    thinking_chars: int; text: str
    tool_executions: list[ToolExecution]
    model_label: str; finish_reason: str | None
    usage: dict                  # input/cache_read/cache_write/output/actual_input
    elapsed_ms: int | None; hook_injected_chars: int | None
    context_fingerprint: str | None

@dataclass(frozen=True)
class Turn:                      # 一条 user 消息到下一条 user 之前
    index: int; query: str; steps: list[Step]
    final_text: str              # 最后一个 step 的 text
    usage_totals: dict; elapsed_ms: int
    started_at: str | None; failed: dict | None; interrupted: bool  # trace 增强

@dataclass(frozen=True)
class SessionTrajectory:
    session_id: str; agent_id: str; cwd: str; title: str | None
    created_at: str; updated_at: str
    turns: list[Turn]; compaction_steps: list[int]
    session_usage: dict; trace_available: bool
```

- **turn 切分**：active_path 消息序按 `UserMessage` 分段；段内每条 `AssistantMessage` 为一个 step；`ToolResultMessage` 按 `tool_call_id` 回填到所属 step 的 `ToolExecution`。
- **上下文变化**：前端由各 step 的 `usage.actual_input` 序列绘制曲线（compaction 处标记），不另存字段。
- **配对失败容错**：孤儿 tool_result（找不到 call）与无结果的 call 都保留并标注，不丢弃、不抛异常。

## 5. HTML 平台（单文件、离线）

- 自包含：数据以 `<script type="application/json">` 内嵌；vanilla JS + inline CSS + inline SVG 图表；零外部请求（CSP `default-src 'none'` 亦可打开）。
- 布局：左侧会话列表（时间倒序，仅含 ≥1 条 message 的会话；标题/agent/cwd/消息数/总 token）；右侧会话详情：
  1. **概览卡**：总 turn 数、总 step、工具调用次数、错误次数、session usage 四分项 + actual_input、总耗时、缓存命中率 `cache_read / actual_input`。
  2. **上下文曲线**：x = step（全会话连续），y = actual_input 堆叠（input / cache_read / cache_write），compaction 竖线标记。
  3. **turn 时间线**：query → 逐 step（模型、耗时、token 徽标、finish_reason）→ 工具执行（名称、参数 JSON、结果预览、is_error 红标、duration）→ 最终回复。turn_failed / interrupted 显著标注。
- 中文界面；估计/增强数据一律带来源标注（「trace 增强」）。

## 6. CLI 合同

```
pickel observe [--session ID]... [--out PATH] [--limit N]
```

- 无 `--session`：导出最近 `--limit`（默认 20）个**含消息**的会话进入同一 HTML。
- `--out` 默认 `./pickel-observe.html`；写完打印绝对路径。
- 退出码：无任何可导出会话 → 非 0 并提示。

## 7. 测试策略

- `tests/observe/test_collector.py`：turn 切分矩阵（单 turn 多 step、多 turn、tool 配对、孤儿 result、无 usage 的 assistant、compaction 入 indices）；usage 合计与 `turn_usage` 口径一致（actual_input = in + cache_read + cache_write）。
- `tests/observe/test_trace_reader.py`：白名单字段（不读 user_text/text/usage 等对话与用量字段——红线）；文件缺失 → 空增强；坏行跳过。
- `tests/observe/test_html_report.py`：输出含内嵌 JSON 且可解析；无外部 URL（`http://`/`https://` 仅允许出现在数据正文里，标签属性中不得有）；空会话列表报错路径。
- `tests/cli/test_observe_command.py`：临时 SQLite 造数据 → 命令生成文件、内容含 session_id。
- 变异实测（团队复审要求）：对 collector 的 turn 切分与配对逻辑各做一次手工变异（如把 `tool_call_id` 配对改成按序配对），确认测试确实咬人。

## 8. 分期

| 期 | 内容 |
|----|------|
| **V1** | model + collector + trace_reader + html_report + CLI，上述全部功能 |
| V2（后话） | `--serve` 本地只读 server、diff 两次运行、per-skill 上下文分栏（复用 measure）、导出 JSON |

## 9. O4 — RequestDigest(2026-07-27 增补,用户批准)

**问题**:完整 wire Request 不落库(observability-design §3.3),观测平台看不到「当时发了什么样的请求」。
**方案**:react 在 before_request hook 之后、generate 之前发 `request_digest` runtime 事件——**只含摘要,不含正文**:

| 字段 | 内容 |
|------|------|
| `system_sections` | `[{name, chars}]` 每段名称与字符数(behavior / skills_guidance / skills_catalog) |
| `tool_names` | 工具名列表(不含 description/schema) |
| `message_count` | 请求消息条数 |
| `request_chars` | hook 后整个 Request 的字符数(与 hook_injected_chars 同口径) |
| `hook_injected_chars` | 本次 hook 改写量 |

- 落盘走既有 JsonlTraceSink,开关即 `PICKEL_TRACE`(不新增开关);显式非真源。
- trace_reader 白名单扩展:按 turn_started 分组收 digest 序列;collector 仅当 turn 数与组数、组内 digest 数与 step 数**都匹配**时按序回填 `Step.request_digest`,否则跳过(容错)。
- HTML:step 内 details 展示「请求摘要 · trace · 非真源」。
- 红线不变:digest 不含任何 system/messages/tools 正文;白名单测试继续锁死 SECRET 不泄露。

## 10. 自评审记录

- 占位符：无 TBD/TODO。
- 一致性：trace 白名单与红线 5 的解释已在 §2.2 写明边界（只取时序与终态，不取对话/用量）；与 trace_sink「只写不读」的冲突以**新增独立 reader 且白名单**的方式解决，trace_sink 本身不加读接口。
- 范围：单一实施计划可完成（5 个新文件 + 1 处 CLI 注册）。
- 歧义：turn 内 `final_text` 取最后 step 的 text（可能为空，如以工具失败收尾）——明确为「可为空字符串」。
