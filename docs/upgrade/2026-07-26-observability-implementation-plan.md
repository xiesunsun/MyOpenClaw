# 运行可观测性 O1 — 实施计划

> **For agentic workers:** 按任务顺序实现；步骤用 checkbox 跟踪。
> **设计依据：** `docs/upgrade/2026-07-26-observability-design.md`（含审阅修订）
> **审阅记录：** `docs/upgrade/2026-07-26-observability-design-review.md`
> **分支建议：** 自 `feature/context-request-prepare-design` 拉实现分支

**Goal:** 恢复 `/context` 的用量分析能力。total 走「真实 usage 锚 + 尾部估计」三档（§6.1），分栏走本地估计归一化（§6.2），默认零远程调用；删除已坏死的 `runs/context_usage.py`。

**Architecture:** `prepare → ModelContext(多 section)` + `UsageAnchor(从 Session 派生)` → `measure` → `ContextUsage` → `ContextRenderer`。measure 无状态、不写 Session、除 §6.1 C 档外不发网络请求。

**Tech Stack:** Python 3.12、现有 pytest。

---

## 文件地图（目标）

| 路径 | 动作 | 职责 |
|------|------|------|
| `src/pickel/context/prepare.py` | 改 | `resolve_system` 产三段 `SystemSection` |
| `src/pickel/runs/context_usage.py` | **删** | 依赖已删除的 `count_request_tokens`，import 即坏 |
| `tests/runs/test_context_usage.py` | **删** | 8 个用例全 `@unittest.skip` |
| `src/pickel/runs/measure.py` | 新增 | `measure(request, anchor) → ContextUsage`；`ContextUsage` / `ContextCategory` 值对象 |
| `src/pickel/runs/usage_anchor.py` | 新增 | `resolve_anchor(session, run) → UsageAnchor \| None`；锚失效判据 |
| `src/pickel/runs/estimator.py` | 新增 | 本地 token 估计（chars/4 起步），无网络 |
| `src/pickel/cli/context_renderer.py` | 改 | `ContextRenderer` 吃新 `ContextUsage`；删 `ModelContextRenderer` 的计数视图 |
| `src/pickel/cli/chat.py` | 改 | `_render_context_command` 修四项；删 `_last_assistant_metadata` |
| `src/pickel/conversations/agent_message.py` | 改 | `ModelResponseMetadata` 加 `hook_injected_chars` |
| `src/pickel/runs/strategy/react.py` | 改 | 写入 `hook_injected_chars` |
| `tests/context/test_prepare.py` | 加 | system 分段等价性 |
| `tests/runs/test_measure.py` | 新增 | 三档 total、归一化、非负、和恒等 |
| `tests/runs/test_usage_anchor.py` | 新增 | 锚命中/失效矩阵 |
| `tests/cli/test_chat_loop.py` | 改 | `/context` 不触发 recall、last usage 来自 Session |

---

## 阶段总览

```text
O1a  resolve_system 拆三段 section（对外行为零变化，可独立合入）
O1b  删 context_usage.py，新增 measure + UsageAnchor + estimator
O1c  /context 修四项 + 接 ContextRenderer
O1d  hook_injected_chars
```

每阶段结束：`uv run --with pytest python -m pytest tests/ -q` 全绿。

---

## O1a — system 拆三段 section

### Task a.1: `resolve_system` 产出三段

`src/pickel/context/prepare.py:21-35`。`SystemInstructionParts` 已有 `base_instruction` / `skills_guidance` / `skills_catalog`，直接映射：

```python
sections = [
    SystemSection(name=name, text=text)
    for name, text in (
        ("behavior", parts.base_instruction),
        ("skills_guidance", parts.skills_guidance),
        ("skills_catalog", parts.skills_catalog),
    )
    if text
]
return SystemContent(sections=sections)
```

- [ ] 改 `resolve_system`
- [ ] 保留 `SystemContent.from_text`（其它调用方仍用）

### Task a.2: 等价性测试（锁死零行为变化）

`SystemInstructionParts.full_instruction` 与 `SystemContent.as_text()` 都是「过滤空串后 `"\n\n".join`」。

- [ ] `tests/context/test_prepare.py`：断言 `prepare(...).system.as_text() == parts.full_instruction`，覆盖 有 skills / 无 skills / behavior 为空 三种组合
- [ ] 断言无 skills 时 sections 长度为 1，有 skills 时为 3

**验收：** provider 收到的 system 文本逐字节不变；全量测试绿。此阶段可单独提 PR。

---

## O1b — measure + UsageAnchor

### Task b.1: 删除坏死代码

- [ ] `git rm src/pickel/runs/context_usage.py`
- [ ] `git rm tests/runs/test_context_usage.py`
- [ ] 清理 `cli/chat.py` 与 `cli/context_renderer.py` 的 import（`context_renderer.py:7` 依赖其值对象，随 Task b.2 一并换）

### Task b.2: `ContextUsage` 值对象（`src/pickel/runs/measure.py`）

```python
@dataclass(frozen=True)
class ContextCategory:
    key: str            # behavior | skills_guidance | skills_catalog | messages | tools | other
    label: str
    tokens: int         # 恒非负
    details: list[ContextDetail] = field(default_factory=list)

@dataclass(frozen=True)
class ContextUsage:
    model_label: str
    total_tokens: int
    total_source: Literal["anchor", "anchor_plus_tail", "counted", "estimated"]
    max_input_tokens: int | None
    categories: list[ContextCategory]
    free_tokens: int | None
```

- [ ] 定义值对象；`total_source` 决定 UI 的 `measured` / `estimated` 标注
- [ ] 不含任何缓存字段（旧实现的 fingerprint 缓存移入 `UsageAnchor` 的失效判据）

### Task b.3: 本地估计器（`src/pickel/runs/estimator.py`）

- [ ] `estimate_text(text) -> int`：chars/4 起步，单一入口便于日后换本地 tokenizer
- [ ] `estimate_messages(messages) -> int`：遍历 content blocks 取文本；tool 结果与 thinking blocks 一并计入
- [ ] `estimate_tools(tools) -> int`：name + description + `json.dumps(input_schema)`
- [ ] **禁止任何 await / 网络调用**（用测试断言模块不 import provider）

### Task b.4: `UsageAnchor`（`src/pickel/runs/usage_anchor.py`）

```python
@dataclass(frozen=True)
class UsageAnchor:
    tokens: int                    # input + cache_read + cache_write（§5.1）
    trailing_messages: list        # 锚之后的新消息
    fingerprint: str
```

`resolve_anchor(session, run) -> UsageAnchor | None`：

- [ ] 反向遍历 `session.active_path()`，取最近一条带 `metadata.usage` 的 `AssistantMessage`
- [ ] `tokens = (input_tokens or 0) + (cache_read_tokens or 0) + (cache_write_tokens or 0)`；三者全 None 则视为无锚
- [ ] 遍历途中遇 **compaction entry** → 返回 `None`
- [ ] fingerprint = sha256(provider, model, agent_id, system 文本 hash, tools name+schema hash)；与 `metadata.provider/model` 不符 → 返回 `None`
- [ ] `trailing_messages` = 该 assistant 之后的全部消息

**测试矩阵（`tests/runs/test_usage_anchor.py`）：**

- [ ] 无 entry → None
- [ ] 有 assistant 无 usage → None
- [ ] 有 assistant 有 usage，其后无消息 → 命中，trailing 为空
- [ ] 其后有 user 消息 → 命中，trailing 长度 1
- [ ] 中间有 compaction → None
- [ ] model 变化 → None
- [ ] tools 集合变化 → None
- [ ] usage 只有 cache_read 无 input → tokens 正确不为 0

### Task b.5: `measure`

```python
async def measure(*, request, anchor, provider, model_config) -> ContextUsage
```

- [ ] **total 三档**（§6.1）：
      A `anchor and not anchor.trailing_messages` → `anchor.tokens`，`total_source="anchor"`
      B `anchor and anchor.trailing_messages` → `anchor.tokens + estimate(trailing)`，`"anchor_plus_tail"`
      C 无锚 → `await provider.count_context_tokens(request)`，`"counted"`；返回 None 则本地估计整份 request，`"estimated"`
- [ ] **空 messages 时禁止走 C 档远程调用**（Anthropic `count_tokens` 要求 messages 非空），直接本地估计
- [ ] **分栏**（§6.2）：逐 section / messages / tools 本地估计得 `raw`，`scale = total / sum(raw)`（`sum(raw) == 0` 时跳过），栏位 `round(raw*scale)` 并截断到 ≥0，`other = total - sum(栏位)`（可为 0，不为负则保留，为负时反向从最大栏位扣减）
- [ ] per-skill 明细：对 `skills_catalog` section 按行拆分本地估计，**不发起任何远程差分**
- [ ] `free = max_input_tokens - total`（`max_input_tokens is None` → `None`）

**测试（`tests/runs/test_measure.py`）：**

- [ ] A 档：provider 的 `count_context_tokens` 被断言 **未调用**
- [ ] B 档：total == anchor + 尾部估计；provider 未调用
- [ ] C 档：provider 调用 **恰好 1 次**
- [ ] C 档 provider 返回 None → `total_source == "estimated"`，不抛异常
- [ ] 空会话：provider 未调用，messages 栏为 0，total > 0（system + tools）
- [ ] 归一化：`sum(categories.tokens) == total`（含 other）
- [ ] 所有栏位 `tokens >= 0`
- [ ] 构造 raw 之和远大于 total 的场景（scale < 1），断言无负值、和仍恒等

---

## O1c — `/context` 修复

### Task c.1: `_render_context_command`（`src/pickel/cli/chat.py:496-543`）

- [ ] `recall_sources=[]`（现为 `run.recall_sources`，会触发远程 OV recall，违反 §7.3/§11.6）
- [ ] `unit_window=run.unit_window`（删掉硬编码 fallback `5`）
- [ ] 删除 `self._last_assistant_metadata` 内存变量（含 `chat.py:588` 的写入），改调 `resolve_anchor` / 从 `session.active_path()` 派生
- [ ] Source 行加 `recall skipped`

### Task c.2: `Last turn` 合计

- [ ] 从 active_path 尾部回溯到最近一条 `UserMessage`，合计其后全部 assistant 的 usage 与 `elapsed_ms`，记 `steps` 数
- [ ] 展示 `实际输入 = input + cache_read + cache_write`（§5.1），四个分项作为明细
- [ ] Session 合计：扫全 path 加总（§10 O2 会复用，此处先给 turn 级即可，Session 级可留 O2）

### Task c.3: 渲染

- [ ] `ContextRenderer.render(usage)` 吃新 `ContextUsage`
- [ ] 头部显示 `{total}/{max}` + 进度条 + `measured|estimated` 标注
- [ ] `max_input_tokens is None` → 进度条与 Free 显示 `unknown`，不猜测
- [ ] 删除 `ModelContextRenderer` 的 `system_sections=/messages=/tools=` 计数视图（§7.4）

**测试（`tests/cli/test_chat_loop.py`）：**

- [ ] `/context` 时注入一个会抛异常的 recall source，断言 **未被调用**
- [ ] `/context` 后 last usage 来自预置的 Session entry，而非先前 turn 的内存态
- [ ] 新开 CLI 实例读同一 Session，`/context` 仍能显示 last usage（内存态做不到，锁死回归）
- [ ] prepare 抛异常时降级显示，不崩

---

## O1d — `hook_injected_chars`

### Task d.1: metadata 字段

- [ ] `ModelResponseMetadata` 加 `hook_injected_chars: int | None = None`
- [ ] `_metadata_to_dict` / `_metadata_from_dict` 各加一行（`.get` 读取，老 entry 兼容）
- [ ] 测试：反序列化缺该键的旧 payload 不报错，值为 None

### Task d.2: 写入

- [ ] `react.py` 在 before_request 前后各算一次 Request 文本长度，差值写入 metadata
- [ ] 无 hook 或无改写时为 `0`（区别于 `None` = 该字段早于本次升级）
- [ ] `/context` 的 Last turn 栏展示该值（非 0 时才显示）

---

## 验收

- [ ] `uv run --with pytest python -m pytest tests/ -q` 全绿
- [ ] `grep -rn "count_request_tokens\|_last_assistant_metadata" src/` 无结果
- [ ] 手工：空会话 `/context` → 有 system/tools 数字，无远程调用
- [ ] 手工：一轮对话后 `/context` → `measured`，provider 无 count 调用
- [ ] 手工：`/model` 切换后 `/context` → 回落 C 档，恰好 1 次远程调用
- [ ] 手工：开启 OV session recall 后 `/context` → 无 OV 请求

---

## 不在 O1 范围

Session 级 usage 合计与轨迹摘要（O2）、footer（O2，复用同一 measure）、metadata 补 thinking（O3）、RequestDigest（O4）、本地 tokenizer 替换 chars/4（`estimator.py` 已留单一入口）。
