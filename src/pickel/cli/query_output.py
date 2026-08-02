"""Query Surface 的稳定 text/json/jsonl 输出合同。"""

from __future__ import annotations

import json
from dataclasses import asdict

from pickel.app.runtime_models import TurnResult
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent, content_blocks_to_list
from pickel.runs.runtime_events import RuntimeEventBase, TurnFailed

SCHEMA_VERSION = 1

_PUBLIC_EVENT_TYPES = {
    "turn_started": "turn.started",
    "step_started": "step.started",
    "tool_call_started": "tool.started",
    "tool_call_completed": "tool.completed",
    "assistant_message": "message.completed",
    "turn_completed": "turn.completed",
    "turn_failed": "turn.failed",
    "thinking_delta": "thinking.delta",
    "text_delta": "message.delta",
    "tool_call_args_delta": "tool.arguments.delta",
    "request_digest": "request.prepared",
    "turn_interrupted": "turn.interrupted",
}


def assistant_text(message: AssistantMessage | None) -> str:
    if message is None:
        return ""
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(block, TextContent) and block.text
    )


def encode_result_text(result: TurnResult) -> str:
    return assistant_text(result.message)


def result_to_dict(result: TurnResult) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": result.status,
        "session_id": result.session_id,
        "turn_id": result.turn_id,
        "message": (
            {
                "role": result.message.role,
                "content": content_blocks_to_list(list(result.message.content)),
            }
            if result.message is not None
            else None
        ),
        "usage": (
            {
                **asdict(result.usage),
                "actual_input_tokens": result.usage.actual_input_tokens,
            }
            if result.usage is not None
            else None
        ),
        "elapsed_ms": result.elapsed_ms,
        "error": asdict(result.error) if result.error is not None else None,
    }


def encode_result_json(result: TurnResult) -> str:
    return json.dumps(result_to_dict(result), ensure_ascii=False, separators=(",", ":"))


def event_to_dict(event: RuntimeEventBase) -> dict:
    payload = event.to_dict()
    internal_type = payload.pop("event_type")
    if isinstance(event, TurnFailed):
        payload.pop("traceback", None)
    public_type = _PUBLIC_EVENT_TYPES.get(internal_type)
    if public_type is None:
        payload["runtime_event_type"] = internal_type
        public_type = "runtime.event"
    return {
        "schema_version": SCHEMA_VERSION,
        "type": public_type,
        **payload,
    }


def encode_event_jsonl(event: RuntimeEventBase) -> str:
    return json.dumps(event_to_dict(event), ensure_ascii=False, separators=(",", ":"))
