"""TurnUsage：从 Session 派生的真实 API usage 合计（设计 §5 / §7.2）。"""

from __future__ import annotations

from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.runs.turn_usage import last_turn_usage, session_usage


def _assistant(
    *,
    text: str = "ok",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read: int | None = None,
    cache_write: int | None = None,
    elapsed_ms: int = 500,
    hook_injected_chars: int | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        metadata=ModelResponseMetadata(
            provider="anthropic",
            model="claude-sonnet-5",
            elapsed_ms=elapsed_ms,
            hook_injected_chars=hook_injected_chars,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
        ),
    )


def test_no_usage_returns_none():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))

    assert last_turn_usage(session) is None
    assert session_usage(session) is None


def test_last_turn_sums_all_steps_in_turn():
    """ReAct 多 step：只显示最后一次会系统性低估本轮成本。"""
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        AssistantMessage(
            content=[ToolCallContent(id="c1", name="echo", arguments={})],
            metadata=_assistant(input_tokens=1000, output_tokens=20).metadata,
        )
    )
    session.append_tool_result(
        ToolResultMessage(tool_call_id="c1", tool_name="echo", content=[TextContent(text="r")])
    )
    session.append_assistant(_assistant(input_tokens=1200, output_tokens=30))

    turn = last_turn_usage(session)

    assert turn.steps == 2
    assert turn.input_tokens == 2200
    assert turn.output_tokens == 50
    assert turn.elapsed_ms == 1000


def test_last_turn_excludes_previous_turns():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="one")]))
    session.append_assistant(_assistant(input_tokens=100, output_tokens=10))
    session.append_user(UserMessage(content=[TextContent(text="two")]))
    session.append_assistant(_assistant(input_tokens=300, output_tokens=30))

    turn = last_turn_usage(session)

    assert turn.steps == 1
    assert turn.input_tokens == 300


def test_actual_input_includes_cache():
    """§5.1：input_tokens 不含 cache，展示口径必须给合计。"""
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        _assistant(input_tokens=100, cache_read=8000, cache_write=200)
    )

    turn = last_turn_usage(session)

    assert turn.input_tokens == 100
    assert turn.cache_read_tokens == 8000
    assert turn.cache_write_tokens == 200
    assert turn.actual_input_tokens == 8300


def test_session_usage_sums_all_turns():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="one")]))
    session.append_assistant(_assistant(input_tokens=100, output_tokens=10))
    session.append_user(UserMessage(content=[TextContent(text="two")]))
    session.append_assistant(_assistant(input_tokens=300, output_tokens=30))

    total = session_usage(session)

    assert total.steps == 2
    assert total.input_tokens == 400
    assert total.output_tokens == 40


def test_hook_injected_chars_is_summed():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(_assistant(hook_injected_chars=120))

    assert last_turn_usage(session).hook_injected_chars == 120


def test_model_label_from_last_step():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(_assistant())

    assert last_turn_usage(session).model_label == "anthropic / claude-sonnet-5"
