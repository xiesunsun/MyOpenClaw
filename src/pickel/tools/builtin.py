from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="echo",
    description="Return the provided text back to the agent.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back.",
            }
        },
        "required": ["text"],
    },
)
async def echo(text: str, context: ToolExecutionContext) -> str:
    return text


@tool(
    name="tool_set_active",
    description=(
        "Narrow or restore which tools are exposed to you. "
        "Changes take effect on the NEXT turn, not the current one. "
        "You can only disable or re-enable tools already granted to this agent; "
        "you cannot add new tools."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool names to hide from yourself starting next turn.",
            },
            "enable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Previously disabled tool names to restore.",
            },
        },
    },
)
async def tool_set_active(
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    control = context.services.activation_control
    if control is None:
        return ToolExecutionResult(
            content="Activation control is not available in this context.",
            is_error=True,
        )

    to_disable = [str(name) for name in (arguments.get("disable") or [])]
    to_enable = [str(name) for name in (arguments.get("enable") or [])]
    if not to_disable and not to_enable:
        return ToolExecutionResult(
            content="Nothing to do: provide 'disable' and/or 'enable'.",
            is_error=True,
        )

    # 只能在人给的白名单内恢复，永远不能扩张
    allowed = control.allowed_names()
    unauthorized = sorted({name for name in to_enable if name not in allowed})
    if unauthorized:
        return ToolExecutionResult(
            content=(
                f"Not granted to this agent: {', '.join(unauthorized)}. "
                "tool_set_active can only restore tools already in the agent's allowlist."
            ),
            is_error=True,
        )

    if to_disable:
        control.disable_tools(to_disable)
    if to_enable:
        control.enable_tools(to_enable)

    changes = []
    if to_disable:
        changes.append(f"disabled {', '.join(sorted(set(to_disable)))}")
    if to_enable:
        changes.append(f"enabled {', '.join(sorted(set(to_enable)))}")
    return ToolExecutionResult(
        content=f"Tool activation updated ({'; '.join(changes)}). Takes effect next turn.",
        metadata={"disabled": sorted(set(to_disable)), "enabled": sorted(set(to_enable))},
    )
