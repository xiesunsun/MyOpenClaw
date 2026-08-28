from pickel.context.context_usage import (
    estimate_context_usage,
    model_context_fingerprint,
)
from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock


def test_tool_schema_estimate_uses_json_projection_of_frozen_schema() -> None:
    context = ModelContext(
        system=SystemContent(),
        messages=(),
        tools=(
            ToolDefinition(
                name="lookup",
                description="查询",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
            ),
        ),
    )

    usage = estimate_context_usage(
        context,
        model_label="openai / gpt-5.6-luna",
        max_input_tokens=1000,
    )

    tools = next(category for category in usage.categories if category.key == "tools")
    assert tools.tokens > 0
    assert usage.total_tokens >= tools.tokens


def test_latest_provider_usage_anchors_matching_context_prefix() -> None:
    prefix = ModelContext(
        system=SystemContent.from_text("system"),
        messages=(UserMessage(content=(TextBlock("hello"),)),),
    )
    assistant = AssistantMessage(
        content=(TextBlock("answer"),),
        metadata=ModelResponseMetadata(
            provider="opencode-go",
            model="glm-5.3-flash",
            usage=ModelUsage(input_tokens=80, output_tokens=20, total_tokens=100),
            context_fingerprint=model_context_fingerprint(prefix),
            hook_injected_chars=0,
        ),
    )
    context = ModelContext(
        system=prefix.system,
        messages=(*prefix.messages, assistant),
    )

    usage = estimate_context_usage(
        context,
        model_label="opencode-go / glm-5.3-flash",
        max_input_tokens=1000,
    )

    assert usage.total_tokens == 100
    assert usage.total_source == "anchor"
    assert usage.free_tokens == 900


def test_anchor_estimates_only_messages_after_measured_assistant() -> None:
    prefix = ModelContext(
        system=SystemContent.from_text("system"),
        messages=(UserMessage(content=(TextBlock("hello"),)),),
    )
    assistant = AssistantMessage(
        content=(TextBlock("answer"),),
        metadata=ModelResponseMetadata(
            provider="anthropic",
            model="claude-test",
            usage=ModelUsage(
                input_tokens=40,
                output_tokens=10,
                cache_read_tokens=30,
                cache_write_tokens=20,
                total_tokens=50,
            ),
            context_fingerprint=model_context_fingerprint(prefix),
            hook_injected_chars=0,
        ),
    )
    context = ModelContext(
        system=prefix.system,
        messages=(
            *prefix.messages,
            assistant,
            UserMessage(content=(TextBlock("next"),)),
        ),
    )

    usage = estimate_context_usage(
        context,
        model_label="anthropic / claude-test",
        max_input_tokens=1000,
    )

    assert usage.total_tokens > 100
    assert usage.total_source == "anchor_plus_tail"


def test_changed_prefix_invalidates_usage_anchor() -> None:
    original = ModelContext(
        system=SystemContent.from_text("old system"),
        messages=(UserMessage(content=(TextBlock("hello"),)),),
    )
    assistant = AssistantMessage(
        content=(TextBlock("answer"),),
        metadata=ModelResponseMetadata(
            provider="opencode-go",
            model="glm-5.3-flash",
            usage=ModelUsage(total_tokens=100),
            context_fingerprint=model_context_fingerprint(original),
            hook_injected_chars=0,
        ),
    )
    changed = ModelContext(
        system=SystemContent.from_text("new system"),
        messages=(*original.messages, assistant),
    )

    usage = estimate_context_usage(
        changed,
        model_label="opencode-go / glm-5.3-flash",
        max_input_tokens=1000,
    )

    assert usage.total_source == "estimated"
    assert usage.total_tokens != 100
