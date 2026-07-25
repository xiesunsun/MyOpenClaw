from pickel.runs.events import RuntimeEvent, RuntimeEventHandler, RuntimeEventType
from pickel.shared.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)

__all__ = [
    "ExecutionStrategy",
    "FinishReason",
    "GenerateRequest",
    "GenerateResult",
    "ReActStrategy",
    "Run",
    "RuntimeEvent",
    "RuntimeEventHandler",
    "RuntimeEventType",
    "TokenUsage",
]


def __getattr__(name: str):
    if name == "Run":
        from pickel.runs.run import Run

        return Run

    if name in {"ExecutionStrategy", "ReActStrategy"}:
        from pickel.runs.strategy import ExecutionStrategy, ReActStrategy

        return {"ExecutionStrategy": ExecutionStrategy, "ReActStrategy": ReActStrategy}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
