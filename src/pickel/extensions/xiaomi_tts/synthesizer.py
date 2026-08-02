"""小米 MiMo-V2.5-TTS 语音合成器。"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from typing import Any

import httpx

_BASE_URL = "https://api.xiaomimimo.com/v1"
_MODEL = "mimo-v2.5-tts"


@dataclass(frozen=True)
class SpeechSynthesisRequest:
    text: str
    style: str
    voice: str


@dataclass(frozen=True)
class SynthesizedAudio:
    data: bytes
    media_type: str
    sample_rate: int | None = None
    channels: int | None = None


class SpeechSynthesisError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class XiaomiResponseError(SpeechSynthesisError):
    pass


class XiaomiSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        max_attempts: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._transport = transport

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
    ) -> SynthesizedAudio:
        messages: list[dict[str, str]] = []
        if request.style.strip():
            messages.append({"role": "user", "content": request.style})
        messages.append({"role": "assistant", "content": request.text})

        for attempt in range(1, self._max_attempts + 1):
            try:
                data = await self._synthesize_once(messages, request.voice)
                return SynthesizedAudio(data=data, media_type="audio/wav")
            except Exception as exc:  # noqa: BLE001 — 在这里统一判断可重试错误
                if attempt >= self._max_attempts or not self._is_retryable(exc):
                    if isinstance(exc, SpeechSynthesisError):
                        raise
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                        raise SpeechSynthesisError(
                            f"小米 TTS 请求失败: HTTP {status}",
                            retryable=self._is_retryable(exc),
                        ) from exc
                    if isinstance(exc, httpx.HTTPError):
                        raise SpeechSynthesisError(
                            f"小米 TTS 网络请求失败: {type(exc).__name__}",
                            retryable=True,
                        ) from exc
                    raise
                await asyncio.sleep(0.25 * attempt)
        raise AssertionError("unreachable")

    async def _synthesize_once(
        self,
        messages: list[dict[str, str]],
        voice: str,
    ) -> bytes:
        async with httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(
                "/chat/completions",
                headers={"api-key": self._api_key},
                json={
                    "model": _MODEL,
                    "messages": messages,
                    "audio": {"format": "wav", "voice": voice},
                },
            )
            response.raise_for_status()
            return self._decode_audio(response.json())

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, SpeechSynthesisError):
            return exc.retryable
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {429, 502, 503, 504}
        return isinstance(exc, httpx.TransportError)

    @staticmethod
    def _decode_audio(payload: dict[str, Any]) -> bytes:
        try:
            data = payload["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            choices = payload.get("choices", [{}])
            first = choices[0] if isinstance(choices, list) and choices else {}
            message = first.get("message", {}) if isinstance(first, dict) else {}
            request_id = payload.get("id")
            finish_reason = (
                first.get("finish_reason") if isinstance(first, dict) else None
            )
            content = message.get("content") if isinstance(message, dict) else None
            detail = ", ".join(
                item
                for item in (
                    f"request_id={request_id}" if request_id else "",
                    f"finish_reason={finish_reason}" if finish_reason else "",
                    f"message={str(content)[:120]}" if content else "",
                )
                if item
            )
            suffix = f"（{detail}）" if detail else ""
            raise XiaomiResponseError(
                f"小米 TTS 响应缺少音频数据{suffix}",
                retryable=True,
            ) from exc
        if not isinstance(data, str):
            raise XiaomiResponseError(
                "小米 TTS 音频数据格式错误",
                retryable=False,
            )
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise XiaomiResponseError(
                "小米 TTS 音频 Base64 解码失败",
                retryable=False,
            ) from exc
