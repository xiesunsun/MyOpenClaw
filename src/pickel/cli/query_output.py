"""Query Surface 的稳定 text/json/jsonl 输出合同。"""

from __future__ import annotations

import json
from dataclasses import asdict

from pickel.app.runtime_models import AgentRunResult
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock, content_blocks_to_list
from pickel.runtime.runtime_events import RuntimeEventBase, AgentRunFailed

SCHEMA_VERSION = 1

_PUBLIC_EVENT_TYPES = {
    "agent_run_started": "agent_run.started",
    "assistant_message": "message.completed",
    "agent_run_completed": "agent_run.completed",
    "agent_run_failed": "agent_run.failed",
    "thinking_delta": "thinking.delta",
    "text_delta": "message.delta",
    "tool_call_args_delta": "tool.arguments.delta",
    "agent_run_interrupted": "agent_run.interrupted",
}


def assistant_text(message: AssistantMessage | None) -> str:
    if message is None:
        return ""
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(block, TextBlock) and block.text
    )


def encode_result_text(result: AgentRunResult) -> str:
    return assistant_text(result.message)


def result_to_dict(result: AgentRunResult) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": result.status,
        "session_id": result.session_id,
        "operation_id": result.operation_id,
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


def encode_result_json(result: AgentRunResult) -> str:
    return json.dumps(result_to_dict(result), ensure_ascii=False, separators=(",", ":"))


def event_to_dict(event: RuntimeEventBase) -> dict:
    payload = event.to_dict()
    internal_type = payload.pop("event_type")
    if isinstance(event, AgentRunFailed):
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
