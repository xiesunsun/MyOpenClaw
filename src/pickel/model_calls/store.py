"""ModelCall 持久化的窄领域端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.conversation_node import ConversationNode
from pickel.model_calls.content_store import ModelCallContentStore
from pickel.model_calls.model_call import ModelCall, ModelCallStatus
from pickel.operations.agent_run_state import AgentRunState


class ModelCallStore(Protocol):
    @property
    def model_call_content_store(self) -> ModelCallContentStore: ...

    def load_model_call(self, model_call_id: str) -> ModelCall | None: ...

    def list_model_calls(
        self,
        *,
        session_id: str,
        operation_id: str | None = None,
        step_id: str | None = None,
    ) -> tuple[ModelCall, ...]: ...

    def prepare_agent_model_call(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool: ...

    def insert_session_model_call(self, *, model_call: ModelCall) -> None: ...

    def transition_model_call(
        self,
        *,
        model_call: ModelCall,
        expected_status: ModelCallStatus,
    ) -> bool: ...

    def commit_agent_model_response(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool: ...

    def commit_agent_model_processing_failure(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool: ...
