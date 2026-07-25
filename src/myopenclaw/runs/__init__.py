from myopenclaw.runs.events import RuntimeEvent, RuntimeEventHandler, RuntimeEventType
from myopenclaw.shared.generation import (
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
        from myopenclaw.runs.run import Run

        return Run

    if name in {"ExecutionStrategy", "ReActStrategy"}:
        from myopenclaw.runs.strategy import ExecutionStrategy, ReActStrategy

        return {"ExecutionStrategy": ExecutionStrategy, "ReActStrategy": ReActStrategy}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
