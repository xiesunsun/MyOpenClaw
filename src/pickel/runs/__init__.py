from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    RuntimeEventHandler,
    StepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnInterrupted,
    TurnStarted,
)
from pickel.shared.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)

__all__ = [
    "AssistantMessageEvent",
    "EventBus",
    "ExecutionStrategy",
    "FinishReason",
    "GenerateRequest",
    "GenerateResult",
    "ReActStrategy",
    "Run",
    "RuntimeEventBase",
    "RuntimeEventHandler",
    "StepStarted",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "TokenUsage",
    "ToolCallArgsDeltaEvent",
    "ToolCallCompleted",
    "ToolCallStarted",
    "TurnCompleted",
    "TurnFailed",
    "TurnInterrupted",
    "TurnStarted",
]


def __getattr__(name: str):
    if name == "Run":
        from pickel.runs.run import Run

        return Run

    if name in {"ExecutionStrategy", "ReActStrategy"}:
        from pickel.runs.strategy import ExecutionStrategy, ReActStrategy

        return {"ExecutionStrategy": ExecutionStrategy, "ReActStrategy": ReActStrategy}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
