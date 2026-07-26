"""UsageAnchor：从 Session 派生真实 usage 锚与失效判据（设计 §6.1）。"""

from __future__ import annotations

from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.runs.usage_anchor import context_fingerprint, resolve_anchor

PROVIDER = "anthropic"
MODEL = "claude-sonnet-5"


def _request(*, system_text: str = "You are Pickle.", tool_names=("echo",)) -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text(system_text),
        messages=[],
        tools=[
            ToolDefinition(name=name, description=f"{name} tool", input_schema={})
            for name in tool_names
        ],
    )


def _metadata(
    *,
    request: ModelContext,
    input_tokens: int | None = 1000,
    output_tokens: int | None = 50,
    cache_read: int | None = None,
    cache_write: int | None = None,
    provider: str = PROVIDER,
    model: str = MODEL,
    fingerprint: str | None = "auto",
) -> ModelResponseMetadata:
    return ModelResponseMetadata(
        provider=provider,
        model=model,
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
        context_fingerprint=(
            context_fingerprint(request, provider=provider, model=model)
            if fingerprint == "auto"
            else fingerprint
        ),
    )


def _session_with_reply(**metadata_kwargs) -> tuple[Session, ModelContext]:
    request = _request()
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="hello")],
            metadata=_metadata(request=request, **metadata_kwargs),
        )
    )
    return session, request


def _resolve(*, session, request, provider: str = PROVIDER, model: str = MODEL):
    return resolve_anchor(
        session=session,
        request=request,
        provider=provider,
        model=model,
    )


def test_no_entries_returns_none():
    session = Session.create(agent_id="Pickle")

    assert _resolve(session=session, request=_request()) is None


def test_assistant_without_usage_returns_none():
    request = _request()
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="hello")],
            metadata=ModelResponseMetadata(
                provider=PROVIDER,
                model=MODEL,
                context_fingerprint=context_fingerprint(
                    request, provider=PROVIDER, model=MODEL
                ),
            ),
        )
    )

    assert _resolve(session=session, request=request) is None


def test_anchor_hits_with_no_trailing_messages():
    session, request = _session_with_reply()

    anchor = _resolve(session=session, request=request)

    assert anchor is not None
    assert anchor.input_tokens == 1000
    assert anchor.output_tokens == 50
    assert anchor.trailing_messages == []


def test_anchor_next_request_base_includes_assistant_output():
    """下一次请求 = 上次输入 + 上次输出；不含输出会系统性低估。"""
    session, request = _session_with_reply()

    anchor = _resolve(session=session, request=request)

    assert anchor.next_request_base == 1050


def test_anchor_sums_cache_tokens_into_input():
    """§5.1：input_tokens 不含 cache，锚必须取三者之和。"""
    session, request = _session_with_reply(
        input_tokens=100,
        cache_read=8000,
        cache_write=200,
    )

    anchor = _resolve(session=session, request=request)

    assert anchor.input_tokens == 8300


def test_anchor_works_when_only_cache_read_present():
    session, request = _session_with_reply(input_tokens=None, cache_read=8000)

    anchor = _resolve(session=session, request=request)

    assert anchor is not None
    assert anchor.input_tokens == 8000


def test_anchor_collects_trailing_messages():
    session, request = _session_with_reply()
    session.append_user(UserMessage(content=[TextContent(text="next question")]))

    anchor = _resolve(session=session, request=request)

    assert len(anchor.trailing_messages) == 1
    assert isinstance(anchor.trailing_messages[0], UserMessage)


def test_本地合成的assistant被当作trailing_锚仍取上一条真实调用():
    """max-steps 的合成消息（无 metadata）不得让锚失效。

    锚失效 → /context 每次都退回远程 count，是可观测性回归。
    """
    session, request = _session_with_reply()
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="Reached the maximum number of reasoning steps.")]
        )
    )

    anchor = _resolve(session=session, request=request)

    assert anchor is not None
    assert anchor.input_tokens == 1000
    assert anchor.output_tokens == 50
    # 合成消息确实进了下一次请求，故按 trailing 估计而非丢弃
    assert len(anchor.trailing_messages) == 1
    assert isinstance(anchor.trailing_messages[0], AssistantMessage)


def test_有metadata但无usage的assistant仍然整体失效():
    """区分「本地合成」与「模型返回了但没给 usage」：后者保持保守失效。"""
    request = _request()
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="first")],
            metadata=_metadata(request=request),
        )
    )
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="no usage")],
            metadata=ModelResponseMetadata(
                provider=PROVIDER,
                model=MODEL,
                context_fingerprint=context_fingerprint(
                    request, provider=PROVIDER, model=MODEL
                ),
            ),
        )
    )

    assert _resolve(session=session, request=request) is None


def test_anchor_uses_last_assistant_when_multiple_steps():
    request = _request()
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    session.append_assistant(
        AssistantMessage(
            content=[ToolCallContent(id="c1", name="echo", arguments={})],
            metadata=_metadata(request=request, input_tokens=1000, output_tokens=20),
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="echo",
            content=[TextContent(text="result")],
        )
    )
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="done")],
            metadata=_metadata(request=request, input_tokens=1200, output_tokens=30),
        )
    )

    anchor = _resolve(session=session, request=request)

    assert anchor.input_tokens == 1200
    assert anchor.trailing_messages == []


def test_compaction_after_anchor_invalidates():
    session, request = _session_with_reply()
    session.append_compaction({"summary": "s", "first_kept_entry_id": "x"})

    assert _resolve(session=session, request=request) is None


def test_model_change_invalidates():
    session, request = _session_with_reply(model="claude-opus-5")

    assert _resolve(session=session, request=request) is None


def test_provider_change_invalidates():
    session, request = _session_with_reply(provider="google/gemini")

    assert _resolve(session=session, request=request) is None


def test_system_text_change_invalidates():
    """/reload 后 skills catalog 变化 → 锚作废。"""
    session, _ = _session_with_reply()

    changed = _request(system_text="You are Pickle. New skill added.")

    assert _resolve(session=session, request=changed) is None


def test_tools_change_invalidates():
    session, _ = _session_with_reply()

    changed = _request(tool_names=("echo", "shell"))

    assert _resolve(session=session, request=changed) is None


def test_missing_fingerprint_invalidates():
    """升级前写入的旧 entry 无 fingerprint → 保守失效，走远程兜底。"""
    session, request = _session_with_reply(fingerprint=None)

    assert _resolve(session=session, request=request) is None


def test_fingerprint_is_stable_and_order_independent_for_same_request():
    a = _request(tool_names=("echo", "shell"))
    b = _request(tool_names=("shell", "echo"))

    assert context_fingerprint(a, provider=PROVIDER, model=MODEL) == context_fingerprint(
        b, provider=PROVIDER, model=MODEL
    )
