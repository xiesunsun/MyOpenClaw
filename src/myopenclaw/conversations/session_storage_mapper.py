from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview


def session_to_metadata_record(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "created_at": _datetime_to_storage(session.created_at),
        "updated_at": _datetime_to_storage(session.updated_at),
        "status": session.status,
        "remote_session_id": session.remote_session_id,
        "last_synced_message_index": session.last_synced_message_index,
        "last_committed_message_index": session.last_committed_message_index,
        "last_committed_at": _datetime_to_storage(session.last_committed_at),
        "openviking_account_id": session.openviking_account_id,
        "openviking_user_id": session.openviking_user_id,
        "openviking_agent_id": session.openviking_agent_id,
    }


def session_message_to_record(
    *,
    session_id: str,
    message_index: int,
    message: SessionMessage,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "message_index": message_index,
        "payload_json": json.dumps(_message_to_payload(message)),
        "created_at": _datetime_to_storage(created_at),
    }


def session_message_from_record(record: Mapping[str, Any]) -> SessionMessage:
    payload = json.loads(str(record["payload_json"]))
    return _message_from_payload(payload)


def session_from_storage(
    *,
    session_record: Mapping[str, Any],
    message_records: Iterable[Mapping[str, Any]],
) -> Session:
    return Session(
        session_id=str(session_record["session_id"]),
        agent_id=str(session_record["agent_id"]),
        messages=[session_message_from_record(record) for record in message_records],
        created_at=_datetime_from_storage(session_record["created_at"]),
        updated_at=_datetime_from_storage(session_record["updated_at"]),
        status=str(session_record["status"]),
        remote_session_id=_optional_str(session_record["remote_session_id"]),
        last_synced_message_index=_optional_int(
            session_record["last_synced_message_index"]
        ),
        last_committed_message_index=_optional_int(
            _mapping_get(session_record, "last_committed_message_index")
        ),
        last_committed_at=_datetime_from_storage(
            session_record["last_committed_at"]
        ),
        openviking_account_id=_optional_str(
            _mapping_get(session_record, "openviking_account_id")
        ),
        openviking_user_id=_optional_str(
            _mapping_get(session_record, "openviking_user_id")
        ),
        openviking_agent_id=_optional_str(
            _mapping_get(session_record, "openviking_agent_id")
        ),
    )


def build_session_preview(*, session: Session) -> SessionPreview:
    last_message = _preview_text_from_message(
        session.messages[-1] if session.messages else None
    )
    return SessionPreview(
        session_id=session.session_id,
        agent_id=session.agent_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        status=session.status,
        message_count=len(session.messages),
        last_message=last_message,
    )


def session_preview_from_storage_record(record: Mapping[str, Any]) -> SessionPreview:
    last_payload_json = record["last_payload_json"]
    last_message = ""
    if last_payload_json is not None:
        last_message = _preview_text_from_message(
            _message_from_payload(json.loads(str(last_payload_json)))
        )
    return SessionPreview(
        session_id=str(record["session_id"]),
        agent_id=str(record["agent_id"]),
        created_at=_datetime_from_storage(record["created_at"]),
        updated_at=_datetime_from_storage(record["updated_at"]),
        status=str(record["status"]),
        message_count=int(record["message_count"]),
        last_message=last_message,
    )


def _preview_text_from_message(message: SessionMessage | None) -> str:
    if message is None:
        return ""
    content = " ".join(message.content.split())
    if content:
        return content
    if message.tool_call_batch is not None:
        names = ", ".join(call.name for call in message.tool_call_batch.calls)
        return f"[tools] {names}".strip()
    return ""


def _message_to_payload(message: SessionMessage) -> dict[str, Any]:
    return {
        "role": message.role.value,
        "content": message.content,
        "metadata": _metadata_to_payload(message.metadata),
        "tool_call_batch": _tool_batch_to_payload(message.tool_call_batch),
    }


def _message_from_payload(payload: Mapping[str, Any]) -> SessionMessage:
    return SessionMessage(
        role=MessageRole(str(payload["role"])),
        content=str(payload.get("content", "")),
        metadata=_metadata_from_payload(payload.get("metadata")),
        tool_call_batch=_tool_batch_from_payload(payload.get("tool_call_batch")),
    )


def _metadata_to_payload(metadata: MessageMetadata | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "provider": metadata.provider,
        "model": metadata.model,
        "input_tokens": metadata.input_tokens,
        "output_tokens": metadata.output_tokens,
        "total_tokens": metadata.total_tokens,
        "elapsed_ms": metadata.elapsed_ms,
        "provider_finish_reason": metadata.provider_finish_reason,
        "provider_finish_message": metadata.provider_finish_message,
        "provider_response_id": metadata.provider_response_id,
        "provider_model_version": metadata.provider_model_version,
    }


def _metadata_from_payload(payload: Any) -> MessageMetadata | None:
    if payload is None:
        return None
    return MessageMetadata(
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        input_tokens=_optional_int(payload.get("input_tokens")),
        output_tokens=_optional_int(payload.get("output_tokens")),
        total_tokens=_optional_int(payload.get("total_tokens")),
        elapsed_ms=_optional_int(payload.get("elapsed_ms")),
        provider_finish_reason=_optional_str(payload.get("provider_finish_reason")),
        provider_finish_message=_optional_str(payload.get("provider_finish_message")),
        provider_response_id=_optional_str(payload.get("provider_response_id")),
        provider_model_version=_optional_str(payload.get("provider_model_version")),
    )


def _tool_batch_to_payload(batch: ToolCallBatch | None) -> dict[str, Any] | None:
    if batch is None:
        return None
    return {
        "batch_id": batch.batch_id,
        "step_index": batch.step_index,
        "calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "thought_signature": _encode_bytes(call.thought_signature),
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


def _tool_batch_from_payload(payload: Any) -> ToolCallBatch | None:
    if payload is None:
        return None
    return ToolCallBatch(
        batch_id=str(payload["batch_id"]),
        step_index=int(payload["step_index"]),
        calls=[
            ToolCall(
                id=str(call["id"]),
                name=str(call["name"]),
                arguments=dict(call.get("arguments", {})),
                thought_signature=_decode_bytes(call.get("thought_signature")),
            )
            for call in payload.get("calls", [])
        ],
        results=[
            ToolCallResult(
                call_id=str(result["call_id"]),
                content=str(result.get("content", "")),
                is_error=bool(result.get("is_error", False)),
                metadata=dict(result.get("metadata", {})),
            )
            for result in payload.get("results", [])
        ],
    )


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    return base64.b64decode(str(value).encode("ascii"))


def _datetime_to_storage(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_storage(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _mapping_get(record: Mapping[str, Any], key: str) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return None
