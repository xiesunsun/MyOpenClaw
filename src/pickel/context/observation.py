"""ContextObservation：/context 观测快照。"""

from __future__ import annotations

from dataclasses import dataclass

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import ModelResponseMetadata


@dataclass
class ContextObservation:
    model_context: ModelContext | None
    predicted: bool
    assistant_metadata: ModelResponseMetadata | None = None
    note: str | None = None
