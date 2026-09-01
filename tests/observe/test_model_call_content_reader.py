from __future__ import annotations

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.content_store import (
    InMemoryModelCallContentStore,
    ModelCallContentRef,
)
from pickel.observe.model_call_content_reader import ModelCallContentReader


def test_model_call_content_reader_reads_valid_content() -> None:
    store = InMemoryModelCallContentStore()
    reader = ModelCallContentReader(store)

    req = RequestContent(
        model_context=ModelContext(
            system=SystemContent.from_text("sys instruction"),
            messages=(),
            tools=(),
        ),
        wire_request={"model": "test-model", "messages": []},
    )
    req_bytes = encode_request_content(req)
    req_ref = store.put(req_bytes)

    resp = ResponseContent(
        partial=False,
        provider_response={"choices": [{"message": {"content": "ok"}}]},
        assistant_message=AssistantMessage(
            (TextBlock("ok"),),
            metadata=ModelResponseMetadata(
                provider="test",
                model="test-model",
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_tokens=40,
                ),
            ),
        ),
    )
    resp_bytes = encode_response_content(resp)
    resp_ref = store.put(resp_bytes)

    req_result = reader.read_request_content(req_ref.to_string())
    assert req_result.is_ok
    assert req_result.content is not None
    assert req_result.content.model_context.system.as_text() == "sys instruction"

    resp_result = reader.read_response_content(resp_ref.to_string())
    assert resp_result.is_ok
    assert resp_result.content is not None
    assert resp_result.content.assistant_message.metadata.usage.input_tokens == 100


def test_model_call_content_reader_missing_and_corrupt() -> None:
    store = InMemoryModelCallContentStore()
    reader = ModelCallContentReader(store)

    # 1. 缺失引用
    missing_ref = ModelCallContentRef(
        sha256="1" * 64,
        media_type="application/vnd.pickel.model-call+json",
        encoding="utf-8",
        size_bytes=50,
    )
    missing_res = reader.read_request_content(missing_ref.to_string())
    assert not missing_res.is_ok
    assert missing_res.missing is True
    assert "缺失" in (missing_res.error or "")

    # 2. 非法 ref 字符串
    invalid_res = reader.read_request_content("invalid json string")
    assert not invalid_res.is_ok
    assert invalid_res.corrupted is True

    # 3. None 响应引用返回 None
    none_resp = reader.read_response_content(None)
    assert none_resp.content is None
    assert none_resp.error is None
