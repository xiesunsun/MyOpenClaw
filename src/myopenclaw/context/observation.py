"""ContextObservation：/context 观测快照。"""

from __future__ import annotations

from dataclasses import dataclass

from myopenclaw.context.model_context import ModelContext
from myopenclaw.conversations.agent_message import ModelResponseMetadata


@dataclass
class ContextObservation:
    model_context: ModelContext | None
    predicted: bool
    assistant_metadata: ModelResponseMetadata | None = None
    note: str | None = None
