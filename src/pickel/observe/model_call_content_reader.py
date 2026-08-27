"""从 ModelCallContentStore 读取并验证持久化 RequestContent / ResponseContent。"""

from __future__ import annotations

from dataclasses import dataclass

from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    decode_request_content,
    decode_response_content,
)
from pickel.model_calls.content_store import (
    ModelCallContentCorruptError,
    ModelCallContentMissingError,
    ModelCallContentRef,
    ModelCallContentStore,
)


@dataclass(frozen=True)
class RequestContentReadResult:
    ref_string: str
    ref: ModelCallContentRef | None
    content: RequestContent | None
    error: str | None = None
    missing: bool = False
    corrupted: bool = False

    @property
    def is_ok(self) -> bool:
        return self.content is not None and self.error is None


@dataclass(frozen=True)
class ResponseContentReadResult:
    ref_string: str | None
    ref: ModelCallContentRef | None
    content: ResponseContent | None
    error: str | None = None
    missing: bool = False
    corrupted: bool = False

    @property
    def is_ok(self) -> bool:
        return self.content is not None and self.error is None


class ModelCallContentReader:
    """根据 ModelCall 内容引用读取并检验实际持久化内容。"""

    def __init__(self, store: ModelCallContentStore) -> None:
        self._store = store

    def read_request_content(self, ref_string: str) -> RequestContentReadResult:
        if not ref_string:
            return RequestContentReadResult(
                ref_string="",
                ref=None,
                content=None,
                error="request_content_ref 不能为空",
            )
        try:
            ref = ModelCallContentRef.from_string(ref_string)
        except Exception as exc:
            return RequestContentReadResult(
                ref_string=ref_string,
                ref=None,
                content=None,
                error=f"非法 request_content_ref 格式: {exc}",
                corrupted=True,
            )

        try:
            raw_bytes = self._store.get(ref)
        except ModelCallContentMissingError as exc:
            return RequestContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"ModelCall RequestContent 缺失: {exc}",
                missing=True,
            )
        except ModelCallContentCorruptError as exc:
            return RequestContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"ModelCall RequestContent 损坏: {exc}",
                corrupted=True,
            )
        except Exception as exc:
            return RequestContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"读取 ModelCall RequestContent 失败: {exc}",
            )

        try:
            content = decode_request_content(raw_bytes)
        except Exception as exc:
            return RequestContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"解码 ModelCall RequestContent 失败: {exc}",
                corrupted=True,
            )

        return RequestContentReadResult(
            ref_string=ref_string,
            ref=ref,
            content=content,
        )

    def read_response_content(
        self, ref_string: str | None
    ) -> ResponseContentReadResult:
        if ref_string is None:
            return ResponseContentReadResult(
                ref_string=None,
                ref=None,
                content=None,
            )
        try:
            ref = ModelCallContentRef.from_string(ref_string)
        except Exception as exc:
            return ResponseContentReadResult(
                ref_string=ref_string,
                ref=None,
                content=None,
                error=f"非法 response_content_ref 格式: {exc}",
                corrupted=True,
            )

        try:
            raw_bytes = self._store.get(ref)
        except ModelCallContentMissingError as exc:
            return ResponseContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"ModelCall ResponseContent 缺失: {exc}",
                missing=True,
            )
        except ModelCallContentCorruptError as exc:
            return ResponseContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"ModelCall ResponseContent 损坏: {exc}",
                corrupted=True,
            )
        except Exception as exc:
            return ResponseContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"读取 ModelCall ResponseContent 失败: {exc}",
            )

        try:
            content = decode_response_content(raw_bytes)
        except Exception as exc:
            return ResponseContentReadResult(
                ref_string=ref_string,
                ref=ref,
                content=None,
                error=f"解码 ModelCall ResponseContent 失败: {exc}",
                corrupted=True,
            )

        return ResponseContentReadResult(
            ref_string=ref_string,
            ref=ref,
            content=content,
        )
