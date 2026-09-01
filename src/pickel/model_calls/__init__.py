"""ModelCall 可靠数据底座。"""

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
    ModelCallContentRef,
    ModelCallContentStore,
)
from pickel.model_calls.model_call import ModelCall, ModelCallError
from pickel.model_calls.store import ModelCallStore


def __getattr__(name: str):
    if name == "PreparedModelCall":
        from pickel.providers.prepared import PreparedModelCall

        return PreparedModelCall
    raise AttributeError(name)


__all__ = [
    "FileModelCallContentStore",
    "InMemoryModelCallContentStore",
    "ModelCall",
    "ModelCallContentRef",
    "ModelCallContentStore",
    "ModelCallError",
    "ModelCallStore",
    "PreparedModelCall",
    "RequestContent",
    "ResponseContent",
    "decode_request_content",
    "decode_response_content",
    "encode_request_content",
    "encode_response_content",
]
