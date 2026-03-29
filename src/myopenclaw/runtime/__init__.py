from myopenclaw.runtime.events import RuntimeEvent, RuntimeEventHandler, RuntimeEventType
from myopenclaw.runtime.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)

__all__ = [
    "DefaultProviderResolver",
    "DefaultToolResolver",
    "FinishReason",
    "GenerateRequest",
    "GenerateResult",
    "RuntimeEvent",
    "RuntimeEventHandler",
    "RuntimeEventType",
    "TokenUsage",
    "TurnRunner",
]


def __getattr__(name: str):
    if name in {"DefaultProviderResolver", "DefaultToolResolver", "TurnRunner"}:
        from myopenclaw.runtime.runner import (
            DefaultProviderResolver,
            DefaultToolResolver,
            TurnRunner,
        )

        return {
            "DefaultProviderResolver": DefaultProviderResolver,
            "DefaultToolResolver": DefaultToolResolver,
            "TurnRunner": TurnRunner,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
