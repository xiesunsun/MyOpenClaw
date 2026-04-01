from myopenclaw.runtime.events import RuntimeEvent, RuntimeEventHandler, RuntimeEventType
from myopenclaw.runtime.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)

__all__ = [
    "AgentCoordinator",
    "AgentRuntimeContext",
    "DefaultProviderResolver",
    "DefaultToolResolver",
    "ExecutionStrategy",
    "FinishReason",
    "GenerateRequest",
    "GenerateResult",
    "ReActStrategy",
    "RuntimeEvent",
    "RuntimeEventHandler",
    "RuntimeEventType",
    "TokenUsage",
]

def __getattr__(name: str):
    if name in {"DefaultProviderResolver", "DefaultToolResolver", "AgentRuntimeContext"}:
        from myopenclaw.runtime.context import (
            AgentRuntimeContext,
            DefaultProviderResolver,
            DefaultToolResolver,
        )
        return {
            "AgentRuntimeContext": AgentRuntimeContext,
            "DefaultProviderResolver": DefaultProviderResolver,
            "DefaultToolResolver": DefaultToolResolver,
        }[name]

    if name == "AgentCoordinator":
        from myopenclaw.runtime.coordinator import AgentCoordinator
        return AgentCoordinator

    if name in {"ExecutionStrategy", "ReActStrategy"}:
        from myopenclaw.runtime.strategy import ExecutionStrategy, ReActStrategy
        return {"ExecutionStrategy": ExecutionStrategy, "ReActStrategy": ReActStrategy}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
