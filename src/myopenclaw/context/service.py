from __future__ import annotations

import base64
import hashlib
import json

from myopenclaw.context.message_builder import ConversationContextBuilder
from myopenclaw.context.models import (
    ContextRuntimeStore,
    ConversationWindow,
    EffectiveContextSnapshot,
)
from myopenclaw.context.window_manager import ConversationWindowManager
from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.session import Session


class ConversationContextService:
    def __init__(
        self,
        *,
        window_manager: ConversationWindowManager | None = None,
        message_builder: ConversationContextBuilder | None = None,
    ) -> None:
        self.window_manager = window_manager or ConversationWindowManager()
        self.message_builder = message_builder or ConversationContextBuilder()

    def build_snapshot(
        self,
        *,
        session: Session,
        runtime_store: ContextRuntimeStore,
    ) -> EffectiveContextSnapshot:
        window = runtime_store.windows.setdefault(
            session.session_id,
            ConversationWindow(),
        )
        self.window_manager.sync_with_session(session=session, window=window)
        messages = self.message_builder.build_messages(window=window)
        active_turn_count = 1 if window.current_turn is not None else 0
        trimmed_turn_count = max(
            0,
            window.next_turn_index - len(window.completed_turns) - active_turn_count,
        )
        return EffectiveContextSnapshot(
            session_id=session.session_id,
            messages=messages,
            fingerprint=self._messages_fingerprint(messages),
            completed_turn_count=len(window.completed_turns),
            current_turn_tool_step_count=(
                len(window.current_turn.tool_steps)
                if window.current_turn is not None
                else 0
            ),
            trimmed_turn_count=trimmed_turn_count,
        )

    @classmethod
    def _messages_fingerprint(cls, messages: list[SessionMessage]) -> str:
        payload = [
            cls._serialize_session_message(message)
            for message in messages
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _serialize_session_message(message: SessionMessage) -> dict[str, object]:
        payload: dict[str, object] = {
            "role": message.role.value,
            "content": message.content,
        }
        batch = message.tool_call_batch
        if batch is None:
            return payload

        payload["tool_call_batch"] = {
            "batch_id": batch.batch_id,
            "step_index": batch.step_index,
            "calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "thought_signature": (
                        base64.b64encode(call.thought_signature).decode("ascii")
                        if call.thought_signature is not None
                        else None
                    ),
                }
                for call in batch.calls
            ],
            "results": [
                {
                    "call_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                    "metadata": result.metadata,
                }
                for result in batch.results
            ],
        }
        return payload
