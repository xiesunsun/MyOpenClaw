"""Provider 异常的稳定 Runtime 分类。"""

from __future__ import annotations

import httpx

from pickel.conversations.agent_message import AssistantMessage


class ProviderRequestError(RuntimeError):
    """Runtime 可以持久化的 Provider 请求错误。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class ProviderStreamIncompleteError(ValueError):
    """Provider 已收到部分输出，但流没有可靠终止。"""

    def __init__(
        self,
        *,
        message: str,
        assistant_message: AssistantMessage,
        provider_response: dict,
        http_status: int | None,
    ) -> None:
        super().__init__(message)
        self.assistant_message = assistant_message
        self.provider_response = provider_response
        self.http_status = http_status


_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "prompt is too long",
    "input length exceeds",
    "reduce the length of",
)


def _is_context_overflow(error: Exception) -> bool:
    """按错误文本与 HTTP 响应体识别上下文窗口溢出。

    溢出措辞由各 Provider 适配器维护；此处只匹配稳定的英文短语，
    避免把普通 400 错误误判为可压缩恢复。
    """
    parts = [str(error)]
    response_text = getattr(getattr(error, "response", None), "text", "")
    if isinstance(response_text, str):
        parts.append(response_text)
    haystack = "\n".join(parts).lower()
    return any(marker in haystack for marker in _CONTEXT_OVERFLOW_MARKERS)


def classify_provider_error(error: Exception) -> ProviderRequestError:
    """将不同 SDK/wire 的异常投影为稳定错误。

    不在这里依赖某个 Provider SDK 的具体类，使未安装的可选
    Provider 不会反向影响 Runtime 导入。
    """
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ProviderRequestError(
            code="provider_timeout",
            message="模型请求超时",
            retryable=True,
        )
    if isinstance(error, httpx.TransportError) or error.__class__.__name__ in {
        "APIConnectionError",
        "APITimeoutError",
    }:
        return ProviderRequestError(
            code="provider_connection_error",
            message="无法连接模型服务",
            retryable=True,
        )

    status_code = _status_code(error)
    if _is_context_overflow(error):
        # 溢出重试原请求注定再次失败；Runtime 应走压缩恢复而不是普通重试。
        return ProviderRequestError(
            code="context_window_exceeded",
            message=f"模型上下文窗口溢出: {str(error)[:500]}",
            retryable=False,
            status_code=status_code,
        )
    if status_code is not None:
        retryable = status_code in {408, 409, 425, 429} or status_code >= 500
        return ProviderRequestError(
            code=f"provider_http_{status_code}",
            message=f"模型服务返回 HTTP {status_code}",
            retryable=retryable,
            status_code=status_code,
        )

    if isinstance(error, ValueError):
        if isinstance(error, ProviderStreamIncompleteError):
            return ProviderRequestError(
                code="provider_stream_incomplete",
                message=f"模型流式响应不完整: {error}",
                retryable=True,
                status_code=error.http_status,
            )
        return ProviderRequestError(
            code="provider_response_invalid",
            message=f"模型响应无效: {error}",
            retryable=False,
        )
    return ProviderRequestError(
        code="provider_request_failed",
        message=f"模型请求失败: {error}",
        retryable=False,
    )


def _status_code(error: Exception) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None
