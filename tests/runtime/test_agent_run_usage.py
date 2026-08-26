from datetime import datetime, timezone

import pytest

from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.runtime.agent_run_usage import (
    AgentRunUsage,
    project_agent_run_usage,
)

_CREATED_AT = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _node(
    node_id: str, content, *, content_type: str = "agent_message"
) -> ConversationNode:
    return ConversationNode(
        node_id=node_id,
        session_id="session-1",
        parent_node_id=None,
        content_type=content_type,
        content=content,
        created_at=_CREATED_AT,
    )


def _assistant(
    node_id: str,
    *,
    metadata: ModelResponseMetadata | None = None,
) -> ConversationNode:
    return _node(
        node_id,
        AssistantMessage(content=(TextBlock(text=node_id),), metadata=metadata),
    )


def test_project_agent_run_usage_only_counts_strict_assistant_descendants() -> None:
    nodes = [
        _assistant("input-assistant", metadata=ModelResponseMetadata("p", "m")),
        _node("input", UserMessage(content=(TextBlock(text="input"),))),
        _node("user-child", UserMessage(content=(TextBlock(text="followup"),))),
        _node(
            "tool-child",
            ToolResultMessage(tool_call_id="call-1", tool_name="echo"),
        ),
        _node(
            "compaction",
            HistoryCompaction(summary="old", first_kept_node_id=None),
            content_type="history_compaction",
        ),
        _assistant(
            "assistant-1",
            metadata=ModelResponseMetadata(
                "p",
                "m",
                elapsed_ms=12,
                hook_injected_chars=3,
                usage=ModelUsage(
                    input_tokens=10,
                    cache_read_tokens=2,
                    cache_write_tokens=1,
                    output_tokens=4,
                ),
            ),
        ),
        _assistant("assistant-2"),
    ]

    assert project_agent_run_usage(nodes, "input") == AgentRunUsage(
        steps=2,
        input_tokens=10,
        cache_read_tokens=2,
        cache_write_tokens=1,
        output_tokens=4,
        elapsed_ms=12,
        hook_injected_chars=3,
        model_label="p / m",
    )


def test_project_agent_run_usage_defaults_missing_metadata_and_numbers_to_zero() -> (
    None
):
    nodes = [
        _node("input", UserMessage()),
        _assistant(
            "assistant-1",
            metadata=ModelResponseMetadata(
                "p",
                "m",
                elapsed_ms=None,
                hook_injected_chars=None,
                usage=ModelUsage(
                    input_tokens=None,
                    cache_read_tokens=None,
                    cache_write_tokens=None,
                    output_tokens=None,
                ),
            ),
        ),
        _assistant("assistant-2"),
    ]

    assert project_agent_run_usage(nodes, "input") == AgentRunUsage(
        steps=2, model_label="p / m"
    )


def test_project_agent_run_usage_uses_label_only_when_unique() -> None:
    nodes = [
        _node("input", UserMessage()),
        _assistant("assistant-1", metadata=ModelResponseMetadata("p", "m")),
        _assistant("assistant-2", metadata=ModelResponseMetadata("q", "m")),
    ]

    usage = project_agent_run_usage(nodes, "input")

    assert usage.steps == 2
    assert usage.model_label is None


def test_project_agent_run_usage_rejects_missing_input_node() -> None:
    with pytest.raises(ValueError, match="input_node_id 不在 branch nodes 中"):
        project_agent_run_usage(
            [_node("other", UserMessage())],
            "missing",
        )
