# 运行可观测性设计 — 审阅记录

**审阅对象**：`docs/upgrade/2026-07-26-observability-design.md`（初稿）
**结论**：方向正确，可进 O1；但 §6 measure 合同在当时代码上不可实现，须先改文档
**处置**：本记录中的问题已全部写入设计文档修订版；实施方案见 `2026-07-26-observability-implementation-plan.md`

---

## 做得对的部分

- **真源单一化**（Session entry + `metadata.usage`）与 §3.3 的「不能唯一还原」诚实清单，是这份文档最有价值的部分。承认「历史 wire Request 不可还原」比假装能回放要好得多。
- **拒绝 ObservabilityManager / 平行 telemetry 库**，与「agent 是系统工程对象、需要基础设施而非中台」的立场一致。
- **A/B 两类用量分离**、estimated 标注、禁止估计覆盖账单 —— 正确。
- **分期把「恢复 `/context` 分析」放在「上观测库」之前** —— 优先级正确。

---

## 阻断级（不改文档就无法实现）

### 1. measure 的分栏在当时的 ModelContext 上拆不出来

`prepare.py:35` 用 `SystemContent.from_text(parts.full_instruction)` 只产 **1 个** section，Request 内部没有 behavior / skills 的边界。

§6 要求「System base = 仅 behavior 的增量」「Skills = full system − base system」，而 §8 又要求 `measure` 是 `Request → ContextUsage` 的无状态函数。两者在单 section 的 Request 上二选一。

**处置**：`SystemInstructionParts` 本就有 `base_instruction` / `skills_guidance` / `skills_catalog` 三段，且 `full_instruction` 与 `SystemContent.as_text()` 都是「过滤空串后 `\n\n` 拼接」。改 `resolve_system` 为三段 `SystemSection` 映射即可，provider 收到的文本逐字节不变（O1a）。

### 2. measure 不是无副作用的

`count_context_tokens` 在 Anthropic 是远程 `messages.count_tokens` API（`anthropic.py:67`）。按 §6 的差分算法要 `4 + N(skills)` 次远程往返（旧实现 `context_usage.py:101-111,195-230` 就是这个形状）。20 个 skill = 24 次请求。

§6 只声明「不触发 before_request、不远程 OV recall」，把更大的远程成本漏了。

**处置**：改为锚定法 —— 一次真实 usage 当锚，分栏全部本地估计（§6.1 / §6.2）。见下方「二次修正」。

### 3. `/context` 当时会触发远程 OV recall

`chat.py:517` 传的是 `run.recall_sources`，其中 `OpenVikingRecall` 是远程调用（`boot.py:106-127`）。与 §6/§11 的「不远程 recall」直接冲突，但 §10 的 O1 没把它列为改动项。

**处置**：写入 §7.4 与 §11.6，列为 O1c 必做项。

### 4. 与 `2026-07-12-query-context-harness.md` §15 语义翻转但未声明废止

老合同：「`/context` 优先读取最近一次**真实的** final ModelContext 和 usage」
新合同：「`/context` = 下一 step 预览」

翻转有理由（Request 未落库），但不显式声明取代，实现者会两头对齐。

**处置**：§7.1 加取代声明。

---

## 重要（会导致实现出错或误导用户）

### 5. 空会话在 Anthropic 上算不出来

§6 写「空会话：messages 栏为 0；仍可算 system + tools + total」。但 Anthropic `count_tokens` 要求 messages 非空，provider 层无占位处理。旧实现靠 `normalization_offset`（`context_usage.py:112`）注入占位再扣减 —— 这个知识在初稿里丢了。

**处置**：§6.3 明确「空会话一律走本地估计，不得为凑档位发空 messages 请求」。

### 6. 差分不可加，缺容错规定

多次远程 count 的差值会因 tokenizer 边界、provider 固定开销出现负数或分栏和 ≠ total。初稿没规定负值怎么处理、不一致时以谁为准。

**处置**：§6.2 加「total 为准，栏位截断非负，残差并入 Other，和恒等」。

### 7. 「Last API call」在多 step turn 下会误导

ReAct 一个 turn 多次 generate → 多条 assistant entry。§7.2 只有单数 `Last API call`，用户看到的是最后一 step，会系统性低估本轮成本。

**处置**：§7.2 改为 `Last turn`（按 turn 合计，带 steps 数）。

### 8. `_last_assistant_metadata` 是内存态，违反自家真源原则

`chat.py:588` 把 metadata 存在 CLI 实例变量上，`/new` 或重启即失，且与 §5「A：扫 path 上 assistant metadata」不一致。初稿没点出这条。

**处置**：§7.4 与 §11.8 写入；O1c 改为从 `session.active_path()` 派生。

### 9. cache 语义未定义展示口径

Anthropic 的 `input_tokens` **不含** `cache_read` / `cache_write`。§7.2 把三者并列展示，用户会把 `in` 读成「总输入」，开缓存时低估到十分之一量级。

**处置**：新增 §5.1「实际输入 = input + cache_read + cache_write」，并规定锚也用这个合计。

### 10. `max_input_tokens` 来源与语义未定义

来自 `ModelConfig.max_input_tokens`（`model_config.py:10`），是配置值、可缺省；而模型真实约束是 context window（input+output 共用）。

**处置**：§6.3 注明配置口径、不含 output 预留、None 时显示 unknown。

---

## 次要

| # | 问题 | 处置 |
|---|------|------|
| 11 | §5 的 pi 对照（last usage + trailing estimate）与初稿算法（完整 measure）不同，易被当成要照抄 | §5 明确我们采用同一锚定法 |
| 12 | §2.2 第三条漏字，「非用户可见的第二套平行对话事实库」缺「无」 | 已补 |
| 13 | `/context` 中 `unit_window=getattr(run, "unit_window", 5)` 硬编码 fallback 5（`chat.py:516`） | §7.4 列入，O1c 改为 `run.unit_window` |
| 14 | §10 缺 O1 收尾：`runs/context_usage.py` 依赖已删除的 `count_request_tokens`，import 即坏；测试全 skip | §6.3 与 O1b 写入「直接删除并重写」 |

---

## 二次修正：锚定法（对首轮方案的更正）

首轮建议「一次远程 count 当 total 锚 + 本地分栏归一化」。该方案解掉了阻断 1、2，但**并没有和 pi 对齐**，需要修正。

| | pi | Claude Code | 首轮方案 | 修正后 |
|---|---|---|---|---|
| 占用锚 | 最近 assistant.usage | 未公开 | 每次远程 count | **同 pi：last usage** |
| 尾部新消息 | 本地 estimate | — | 含在 count 里 | 本地 estimate |
| 锚失效时 | 显示 `?/window` | — | 不适用 | 一次远程 count 兜底（**优于 pi**） |
| 分栏 | 无 | 有 | 有 | 有（本地估计归一化） |

修正理由不止「对齐」：**未来 footer 要每轮实时显示占用，远程 count 在那个位置根本不可用**。现在按锚定法定合同，footer 才能直接复用同一 measure，不用重写。

Claude Code 那边：我们对齐的是它的分栏 UI 形态（§7.2 的来源），内部算法未公开，不作为实现依据。

---

## 三次修正：实现阶段发现的两处缺陷

以下两条在 O1 实施过程中发现，设计文档与实施计划均已回写。

### A. 锚必须包含上次的 `output_tokens`

修正后的 §6.1 原写「其后无新消息 → total = anchor」。这漏了 assistant 自身的输出 —— 它在下一轮就是输入的一部分，每轮都会系统性低估一个回复的体量。

正确形式：`next_request_base = input + cache_read + cache_write + output_tokens`。四项全是真实值，A 档仍标 `measured`。

### B. 需要 `context_fingerprint` 字段，且必须记 hook 前的 prepare 输出

初稿与首轮方案都要求「tools 集合变化 → 锚失效」，但历史 entry 里没存当时的 system/tools，比不了。锚判据是空的。

**处置**：`ModelResponseMetadata` 新增 `context_fingerprint`（provider + model + system 文本 + tools 集合的 sha256）。老 entry 无此字段 → 保守失效，走远程兜底。

指纹记 **prepare 输出（hook 前）** 而非实际发出的 Request：`/context` 预览不跑 hook，若记 hook 后的指纹，挂 hook 时锚会永远失效。usage 仍是 hook 后的真实值 —— 锚因此已包含 hook 注入量，两者差额由 `hook_injected_chars` 解释。

---

## 兼容性评估

| 项 | 结论 |
|----|------|
| 后续 Trace / RequestDigest / 导出 / footer | 兼容 —— 真源不动，measure 产值对象不落盘，全是新增派生 |
| 新增分栏（memory / MCP tools / 子 agent） | 兼容 —— 加一个 `SystemSection` 即可，measure 算法不变 |
| `hook_injected_chars` / `context_fingerprint` | 兼容 —— `_metadata_from_dict` 走 `.get`，老 entry 读得动 |
| **按 skill 精确计费归因** | **不兼容** —— 归一化分栏是估计，撑不住。真要精确得靠 provider 侧分段计量，属 §3.3 的不可得清单 |
