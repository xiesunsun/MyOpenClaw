"""classify_provider_error 的稳定投影合同：溢出识别与不可盲重试。"""

import httpx

from pickel.providers.errors import classify_provider_error


def test_value_error_with_overflow_message_maps_to_context_window_exceeded():
    error = classify_provider_error(
        ValueError("This model's maximum context length is 8192 tokens")
    )

    assert error.code == "context_window_exceeded"
    assert error.retryable is False


def test_http_400_with_overflow_body_maps_to_context_window_exceeded():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        text='{"error": {"message": "context length exceeded"}}',
    )

    error = classify_provider_error(
        httpx.HTTPStatusError("Bad Request", request=request, response=response)
    )

    assert error.code == "context_window_exceeded"
    assert error.status_code == 400
    assert error.retryable is False


def test_non_overflow_http_400_keeps_http_code():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        text='{"error": {"message": "invalid api key"}}',
    )

    error = classify_provider_error(
        httpx.HTTPStatusError("Bad Request", request=request, response=response)
    )

    assert error.code == "provider_http_400"
    assert error.retryable is False
