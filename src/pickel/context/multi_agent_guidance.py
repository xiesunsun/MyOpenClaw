"""多 Agent 生命周期的稳定模型指导。"""

from __future__ import annotations

# 这段内容属于 Runtime 的稳定 system context，而不是用户可覆盖的行为模板。
# Parent 与 Child 共用同一份指导；当前 Package 是否公开对应 Tool 仍由冻结
# ToolSnapshot 决定。
MULTI_AGENT_GUIDANCE = """Multi-agent lifecycle:
- `delegate_agent` starts a durable child Session and returns immediately. The child runs independently; this call does not wait for completion.
- When a child Operation reaches a terminal state, Runtime automatically delivers its terminal result to this Parent as an ordinary UserMessage and wakes the Parent. Do not poll for it.
- Continue independent work while a child runs. If there is no independent work, finish the current Operation normally; this Agent becomes idle and will be woken by the child message.
- Never use `bash` sleep, files, or `list_agents` to wait for a child. `list_agents` is only an immediate diagnostic snapshot.
- `send_message` sends a follow-up from a Parent to one direct child. `report` sends an intermediate message from a Child to its direct Parent; neither tool waits for completion. Use only tools exposed by the current Package."""
