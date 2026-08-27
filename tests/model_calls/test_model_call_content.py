from __future__ import annotations

from pathlib import Path

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    decode_request_content,
    decode_response_content,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.content_store import (
    FileModelCallContentStore,
    InMemoryModelCallContentStore,
    ModelCallContentCorruptError,
)


def _context() -> ModelContext:
    return ModelContext(SystemContent.from_text("system"), (), ())


def test_request_content_codec_is_canonical_and_round_trips() -> None:
    content = RequestContent(
        model_context=_context(),
        wire_request={"z": 2, "a": {"nested": True}},
    )

    encoded = encode_request_content(content)

    assert encoded == encode_request_content(content)
    assert encoded.startswith(b'{"model_context":')
    assert decode_request_content(encoded) == content


def test_response_content_codec_round_trips_assistant_message() -> None:
    content = ResponseContent(
        partial=False,
        provider_response={"id": "resp-1", "usage": {"input_tokens": 3}},
        assistant_message=AssistantMessage((TextBlock("ok"),)),
    )

    assert decode_response_content(encode_response_content(content)) == content


@pytest.mark.parametrize("kind", ("memory", "file"))
def test_content_store_is_content_addressed_and_detects_missing_or_corrupt(
    kind: str,
    tmp_path: Path,
) -> None:
    store = (
        InMemoryModelCallContentStore()
        if kind == "memory"
        else FileModelCallContentStore(tmp_path / "model-calls")
    )
    content = encode_request_content(
        RequestContent(model_context=_context(), wire_request={"model": "test"})
    )

    first = store.put(content)
    second = store.put(content)

    assert first == second
    assert store.exists(first)
    assert store.get(first) == content

    if isinstance(store, FileModelCallContentStore):
        path = store.root / "sha256" / first.sha256[:2] / first.sha256[2:]
        path.write_bytes(b"corrupt")
        assert not store.exists(first)
        with pytest.raises(ModelCallContentCorruptError):
            store.get(first)

    store.delete(first)
    assert not store.exists(first)
