# 观测平台（pickel observe）V1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans（本次自主模式内联执行）。步骤用 checkbox 跟踪。
> **设计依据：** `docs/upgrade/2026-07-26-observe-platform-design.md`

**Goal:** 从 Session 真源只读派生完整执行轨迹，导出自包含单文件 HTML 观测平台（CLI：`pickel observe`）。

**Architecture:** `Session(SQLite) → observe/collector → SessionTrajectory 值对象 → observe/html_report → 单文件 HTML`；trace JSONL 经 `observe/trace_reader` 白名单读取做时序增强（非真源）。runtime 不 import observe。

**Tech Stack:** Python 3.12、pytest、typer（已有）、vanilla JS + inline SVG（模板内嵌）。

## Global Constraints

- observe 模块只 import `conversations` / `persistence` / `config.paths` / `runs.trace_sink`（仅 `trace_path` 函数）与 stdlib；禁止 import providers / runs 执行路径 / cli.chat。
- trace 白名单字段：`event_type, seq, occurred_at, turn_id, step_index, tool_call.id, error_type, message(仅 turn_failed), at_step`。禁止读 `user_text / text / usage / arguments / content / partial_json`。
- HTML 输出零外部请求：无 `src=`/`href=` 指向 http(s)。
- actual_input = input + cache_read + cache_write（§5.1 口径）。
- 测试命令：`uv run --with pytest python -m pytest <path> -q`。

---

### Task 1: 轨迹值对象（observe/model.py）

**Files:**
- Create: `src/pickel/observe/__init__.py`（空）
- Create: `src/pickel/observe/model.py`
- Test: `tests/observe/__init__.py`（空）、`tests/observe/test_model.py`

**Interfaces（Produces）:**

```python
@dataclass(frozen=True)
class ToolExecution:
    tool_call_id: str; name: str; arguments: dict[str, Any]
    result_preview: str = ""; is_error: bool = False; orphan: bool = False
    started_at: str | None = None; completed_at: str | None = None
    duration_ms: int | None = None

@dataclass(frozen=True)
class Step:
    index: int; thinking_chars: int; text: str
    tool_executions: list[ToolExecution]
    model_label: str; finish_reason: str | None
    usage: dict[str, int]      # input/cache_read/cache_write/output/actual_input，缺省 0
    elapsed_ms: int | None; hook_injected_chars: int | None
    context_fingerprint: str | None

@dataclass(frozen=True)
class Turn:
    index: int; query: str; steps: list[Step]; final_text: str
    usage_totals: dict[str, int]; elapsed_ms: int
    started_at: str | None = None; failed: dict[str, str] | None = None
    interrupted: bool = False

@dataclass(frozen=True)
class SessionTrajectory:
    session_id: str; agent_id: str; cwd: str; title: str | None
    created_at: str; updated_at: str
    turns: list[Turn]; compaction_steps: list[int]   # 全会话 step 序号处有 compaction
    session_usage: dict[str, int]; trace_available: bool

def trajectory_to_dict(t: SessionTrajectory) -> dict[str, Any]   # dataclasses.asdict
```

- [ ] **Step 1: 失败测试** `tests/observe/test_model.py`：构造含 1 turn/1 step/1 tool 的 `SessionTrajectory`，断言 `json.dumps(trajectory_to_dict(t))` 不抛且往返后 `["turns"][0]["steps"][0]["usage"]["actual_input"]` 正确。
- [ ] **Step 2: 跑测试确认 FAIL**（ModuleNotFoundError）。
- [ ] **Step 3: 实现 model.py**（如上定义 + `trajectory_to_dict = asdict` 包装）。
- [ ] **Step 4: 跑测试 PASS。**
- [ ] **Step 5: Commit** `feat(observe): 轨迹值对象`

### Task 2: 采集器（observe/collector.py）

**Files:**
- Create: `src/pickel/observe/collector.py`
- Test: `tests/observe/test_collector.py`

**Interfaces:**
- Consumes: Task 1 值对象；`Session.active_path()`、`agent_message_from_dict`、`ENTRY_TYPE_MESSAGE/ENTRY_TYPE_COMPACTION`。
- Produces:

```python
def collect_trajectory(session: Session, *, enhancement: "TraceEnhancement | None" = None,
                       result_preview_chars: int = 2000) -> SessionTrajectory
def collect_previews(repository: SessionRepository, *, limit: int = 20) -> list[Session]
    # repository.list(limit=limit) → 过滤 message_count > 0 → repository.load 逐个
```

算法要点：
- 遍历 active_path：`message` entry 反序列化；反序列化失败跳过该 entry（容错）；`compaction` entry 记录当前全会话 step 计数到 `compaction_steps`。
- `UserMessage` → 开新 Turn（query = 拼接 TextContent.text）；首条消息前出现 assistant 时自动开 index=0 的匿名 Turn（query=""）。
- `AssistantMessage` → 新 Step：`thinking_chars = sum(len(b.text) for ThinkingContent)`、`text = 拼接 TextContent`、tool_call blocks → `ToolExecution`（无结果时 result_preview=""）。
- `ToolResultMessage` → 按 `tool_call_id` 回填最近一个匹配的 ToolExecution（result_preview 截断到 `result_preview_chars`、is_error）；找不到 → 追加 `orphan=True` 的 ToolExecution 到当前 Turn 最后一个 Step（无 Step 则丢入匿名 Turn 的空 Step？——不：无任何 Step 时创建 index=0 的空 Step 承载，保证不丢数据）。
- usage：per-step 从 `metadata.usage` 取四项（None→0）算 actual_input；turn 合计与 session 合计逐项累加（口径与 `turn_usage._accumulate` 一致）。
- enhancement 非 None 时：按 `tool_call_id` 回填 tool 时序；turn_markers 数量 == turns 数量时按序回填 started_at/failed/interrupted，不等则跳过 turn 级增强。

- [ ] **Step 1: 失败测试矩阵**（复用 `tests/runs/test_turn_usage.py` 的 `_assistant` 构造手法，本地重写 helper）：
  - 单 turn 双 step + tool 配对：turn.usage_totals 正确、tool result 回填、final_text = 末 step text
  - 多 turn 切分：3 user → 3 turns，index 连续
  - 孤儿 tool_result：orphan=True 保留
  - 无 usage 的 assistant：usage 全 0，不抛
  - compaction entry：`compaction_steps` 记录正确
  - result_preview 截断：3000 字符结果 → len == 2000
  - 首消息为 assistant：匿名 Turn(query="")
- [ ] **Step 2: FAIL 确认。**
- [ ] **Step 3: 实现 collector.py。**
- [ ] **Step 4: PASS + 全量 `uv run --with pytest python -m pytest tests/ -q` 绿。**
- [ ] **Step 5: Commit** `feat(observe): Session→轨迹采集器`

### Task 3: trace 白名单读取（observe/trace_reader.py）

**Files:**
- Create: `src/pickel/observe/trace_reader.py`
- Test: `tests/observe/test_trace_reader.py`

**Interfaces（Produces）:**

```python
@dataclass(frozen=True)
class ToolTiming: started_at: str | None; completed_at: str | None; duration_ms: int | None
@dataclass(frozen=True)
class TurnMarker: started_at: str | None; failed: dict[str, str] | None; interrupted: bool
@dataclass(frozen=True)
class TraceEnhancement:
    tool_timings: dict[str, ToolTiming]; turn_markers: list[TurnMarker]
def read_trace(path: Path) -> TraceEnhancement | None   # 文件不存在 → None
```

- tool_call_started/completed 按 `tool_call["id"]` 配对，duration = completed.occurred_at − started.occurred_at（ms）。
- turn_started 按出现序生成 TurnMarker；turn_failed 取 `error_type` + `message` 写入该 marker.failed；turn_interrupted 置 interrupted。
- 坏 JSON 行跳过。**白名单红线**：不得读取 user_text/text/usage/arguments/content/partial_json。

- [ ] **Step 1: 失败测试**：
  - 手写 6 行 JSONL（turn_started 含 `"user_text": "SECRET"`、tool started/completed 含 `"arguments": {"cmd": "SECRET2"}`、turn_failed、坏行）→ 断言 timings/duration/markers 正确
  - **红线测试**：`json.dumps(asdict(enhancement))` 不含 `SECRET`/`SECRET2`
  - 文件不存在 → None
- [ ] **Step 2: FAIL。** **Step 3: 实现。** **Step 4: PASS。**
- [ ] **Step 5: Commit** `feat(observe): trace 白名单时序增强`

### Task 4: HTML 报告（observe/html_report.py）

**Files:**
- Create: `src/pickel/observe/html_report.py`
- Test: `tests/observe/test_html_report.py`

**Interfaces:**
- Consumes: `trajectory_to_dict`
- Produces: `def render_html(trajectories: list[SessionTrajectory], *, generated_at: str) -> str`

实现要点（**写模板前先 invoke dataviz skill**，图表按其规范）：
- 数据 `<script type="application/json" id="observe-data">`（`json.dumps(..., ensure_ascii=False)`，`</` 转义为 `<\/` 防注入）。
- 单文件：inline CSS + vanilla JS；布局=左会话列表右详情；详情含概览卡（turns/steps/工具次数/错误数/usage 四项+actual_input/耗时/缓存命中率）、上下文 SVG 堆叠曲线（x=step 序，y=input/cache_read/cache_write 堆叠，compaction 竖线）、turn 时间线（query → step 徽标 → 工具卡片(参数/结果/is_error 红标/duration) → final_text；failed/interrupted 标注）。
- 明暗主题各自可读；中文界面；trace 增强字段旁标「trace · 非真源」。

- [ ] **Step 1: 失败测试**：
  - 输出含 `id="observe-data"` 且内嵌 JSON 可 `json.loads` 还原 session_id
  - 输出不含 `src="http` / `href="http`
  - 会话正文含 `</script>` 时输出 JSON 岛不被截断（转义生效）
  - 空列表 → `ValueError`
- [ ] **Step 2: FAIL。** **Step 3: 先 dataviz skill，再实现模板。** **Step 4: PASS。**
- [ ] **Step 5: Commit** `feat(observe): 自包含 HTML 观测平台渲染`

### Task 5: CLI 子命令

**Files:**
- Modify: `src/pickel/cli/main.py`（`@app.command()` observe）
- Test: `tests/cli/test_observe_command.py`

```python
@app.command()
def observe(
    session: list[str] = typer.Option([], "--session", help="指定 session_id，可多次"),
    out: Path = typer.Option(Path("pickel-observe.html"), "--out"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    from pickel.observe.collector import collect_previews, collect_trajectory
    from pickel.observe.trace_reader import read_trace
    from pickel.observe.html_report import render_html
    from pickel.runs.trace_sink import trace_path
    repository = SQLiteSessionRepository(sessions_db_path())
    sessions = ([s for sid in session if (s := repository.load(sid))]
                if session else collect_previews(repository, limit=limit))
    if not sessions:
        typer.echo("没有可导出的会话", err=True); raise typer.Exit(code=1)
    trajectories = [collect_trajectory(s, enhancement=read_trace(trace_path(s.session_id))) for s in sessions]
    out.write_text(render_html(trajectories, generated_at=datetime.now(timezone.utc).isoformat()), encoding="utf-8")
    typer.echo(str(out.resolve()))
```

- [ ] **Step 1: 失败测试**（`typer.testing.CliRunner` + `monkeypatch.setenv("PICKEL_HOME", tmp_path)`）：预置 SQLite 会话（SQLiteSessionRepository.create + append_entries）→ `observe --out x.html` 退出码 0、文件存在、内容含 session_id；空库 → 退出码 1。
- [ ] **Step 2: FAIL。** **Step 3: 实现。** **Step 4: PASS + 全量绿。**
- [ ] **Step 5: Commit** `feat(cli): pickel observe 导出观测平台`

### Task 6: 变异实测 + 真机验证

- [ ] 变异 1：collector 的 tool 配对改按序配对 → `test_collector` 必须变红；还原。
- [ ] 变异 2：trace_reader 白名单放开读 `user_text` 塞进 marker → 红线测试必须变红；还原。
- [ ] 真机：`uv run pickel observe --out /tmp/.../observe.html` 用 ~/.pickel 真实数据生成并抽查内容。
- [ ] 全量 `uv run --with pytest python -m pytest tests/ -q` 绿；Commit（若有修正）。

## Self-Review

- 覆盖：设计 §4（Task 1/2）、§2.2 白名单（Task 3）、§5（Task 4）、§6（Task 5）、§7 测试与变异（各 task + Task 6）。
- 无占位符；类型签名跨任务一致（TraceEnhancement 在 Task 2 前向引用、Task 3 定义，collector 只经参数消费，无循环 import）。
- compaction_steps 命名统一（设计稿 `compaction_indices` 修正为 `compaction_steps`，以 step 序号为准）。
