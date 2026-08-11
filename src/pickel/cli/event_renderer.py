"""事件 → 渲染分派器（E3.1 块模型）。

实际渲染在 `cli/render/`（stream / tool / message），本类只做分派。
所有渲染信息只来自事件——不读 Run/Session/trace。
"""

from rich.console import Console

from pickel.cli.render.message import render_interrupted
from pickel.cli.render.stream import StreamRenderer
from pickel.cli.render.tool import ToolRenderer
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    ModelStepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    ToolCallCompleted,
    ToolCallStarted,
    AgentRunInterrupted,
)
from pickel.tools.base import ToolExecutionResult


class ChatEventRenderer:
    def __init__(
        self, console: Console, *, fallback_model_label: str | None = None
    ) -> None:
        self.console = console
        # AssistantMessageEvent.usage=None 时事件里没有 model 信息，
        # footer 退到装配时注入的 label（E2 遗留修复）
        self._fallback_model_label = fallback_model_label
        self._stream = StreamRenderer(console)
        self._tool = ToolRenderer(console)

    async def handle_event(self, event: RuntimeEventBase) -> None:
        if isinstance(event, TextDeltaEvent):
            self._stream.on_text(event.text)
            return

        if isinstance(event, ThinkingDeltaEvent):
            self._stream.on_thinking(event.text)
            return

        if isinstance(event, ToolCallArgsDeltaEvent):
            # partial_json 拼完前不是合法 JSON，展示半截只会制造噪音
            return

        if isinstance(event, ModelStepStarted):
            # 无边框：Step 行不上屏；预览提交为历史
            self._stream.end()
            return

        if isinstance(event, ToolCallStarted) and event.tool_call is not None:
            self._stream.end()
            self._tool.on_started(event.tool_call, event.envelope.occurred_at)
            return

        if isinstance(event, ToolCallCompleted) and event.tool_call is not None:
            tool_result = event.tool_result or ToolExecutionResult(content="")
            self._tool.on_completed(
                event.tool_call, tool_result, event.envelope.occurred_at
            )
            return

        if isinstance(event, AgentRunInterrupted):
            self._stream.end()
            render_interrupted(self.console)
            return

        if isinstance(event, AssistantMessageEvent):
            self._stream.settle(
                event.text,
                event.usage,
                self._fallback_model_label,
            )
