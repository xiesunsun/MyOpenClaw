# CLI 渲染增强（E3.1）— 落地说明

**状态**：已实施（`feature/context-command-align`）  
**日期**：2026-07-27  
**范围**：仅 CLI 展示（`cli/render/*`、`event_renderer`、`context_renderer`）  
**不在范围**：TUI、Runtime 事件合同、Session 真源

---

## 1. 正文：流式 + footer（无终态 MD）

| 事件 | 行为 |
|------|------|
| `thinking_delta` | `· 思考中……` + dim 白字 |
| `text_delta` | 白字流式上屏 |
| `assistant_message` | **已流式正文** → 只打 footer；**仅 thinking / 非流式** → 白字全文 + footer |

- 不擦屏、不 `rich.Markdown` 重渲 → 避免双份。  
- `**粗体**` 等为模型字面量，终端不解析。

---

## 2. 工具：事件追加（不擦屏）

```text
⏺ shell_exec  command='date "..."'
· running
· ok  (0.2s)          ← failed 为红色
· out  Mon Jul 27 ...
```

| 事件 | 行为 |
|------|------|
| `tool_call_started` | 名与 args **同一行** + `· running` |
| `tool_call_completed` | 追加 `· ok|failed` + `· out`（不重打名） |

Bus 仍只有 started / completed 两条；多行是 UI 模板，不是多事件。

---

## 3. `/context` 面板

- 标题 `Context`；`used / max (pct%)`；菱形 5×20：◆ 占用 / ◇ 剩余。  
- 分栏：System / Messages / Free；Tools / Skills。  
- `Remaining · ~X tokens free`（`free_tokens` 真数据）。  
- **无** Auto-compact 假提示。  
- Turns / Tool calls / Compactions 从 Session 统计。

---

## 4. 文件

| 路径 | 职责 |
|------|------|
| `cli/render/stream.py` | 流式 + settle |
| `cli/render/tool.py` | 工具追加块 |
| `cli/render/message.py` | 白字 assistant / footer / abbrev |
| `cli/event_renderer.py` | 分派 |
| `cli/context_renderer.py` | `/context` 版式 |
| `cli/chat.py` | `/context` 装配与会话统计 |

Runtime / EventBus **不改**。
