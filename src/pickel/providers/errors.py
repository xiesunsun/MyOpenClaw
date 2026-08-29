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
