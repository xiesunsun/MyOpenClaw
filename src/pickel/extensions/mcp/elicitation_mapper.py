"""MCP elicitation 与 Runtime Host call 的类型边界。"""

from __future__ import annotations

from dataclasses import replace

import jsonschema
import mcp.types

from pickel.runtime.host_call_types import (
    EXTERNAL_ACTION_CALL,
    STRUCTURED_INPUT_CALL,
    ExternalActionAnswer,
    ExternalActionRequest,
    HostCallSource,
    StructuredInputAnswer,
    StructuredInputRequest,
)
from pickel.runtime.host_calls import (
    HostCallClient,
    HostCallCompleted,
    HostCallContext,
)


async def resolve_elicitation(
    params: mcp.types.ElicitRequestParams,
    *,
    host_calls: HostCallClient,
    context: HostCallContext,
    server_name: str,
    tool_name: str,
) -> mcp.types.ElicitResult:
    source = HostCallSource(kind="mcp", name=server_name, operation=tool_name)
    if isinstance(params, mcp.types.ElicitRequestFormParams):
        request = StructuredInputRequest(
            source=source,
            title=f"MCP input: {server_name}/{tool_name}",
            message=params.message,
            schema=dict(params.requested_schema),
        )
        outcome = await host_calls.call(STRUCTURED_INPUT_CALL, request, context)
        if not isinstance(outcome, HostCallCompleted):
            return mcp.types.ElicitResult(action="cancel")
        answer: StructuredInputAnswer = outcome.value
        if answer.action == "accept":
            content = answer.content or {}
            try:
                jsonschema.validate(content, params.requested_schema)
            except jsonschema.ValidationError:
                return mcp.types.ElicitResult(action="cancel")
            return mcp.types.ElicitResult(action="accept", content=content)
        return mcp.types.ElicitResult(action=answer.action)

    request = ExternalActionRequest(
        source=source,
        title=f"MCP external action: {server_name}/{tool_name}",
        message=params.message,
        url=params.url,
    )
    # 同一 MCP tool call 的不同 embedded request 必须有独立 call id。
    outcome = await host_calls.call(
        EXTERNAL_ACTION_CALL,
        request,
        replace(context, call_id=f"{context.call_id}:url"),
    )
    if not isinstance(outcome, HostCallCompleted):
        return mcp.types.ElicitResult(action="cancel")
    answer: ExternalActionAnswer = outcome.value
    return mcp.types.ElicitResult(action=answer.action)
