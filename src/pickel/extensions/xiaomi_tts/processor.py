"""把完成的回答转换成音频输出。"""

from __future__ import annotations

import asyncio
import re

from pickel.shared.conversation_output import (
    AudioContent,
    AudioOutputFailed,
    AudioOutputReady,
)
from pickel.extensions.xiaomi_tts.synthesizer import (
    SpeechSynthesisRequest,
    SpeechSynthesisError,
    XiaomiSpeechSynthesizer,
)
from pickel.extensions.xiaomi_tts.config import XiaomiTtsConfig
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    TurnCompleted,
    TurnFailed,
    TurnInterrupted,
)

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_LINK = re.compile(r"\[([^]]+)]\([^)]+\)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def prepare_speech_text(text: str, *, max_chars: int) -> str:
    """删除不适合朗读的 Markdown，并限制发送长度。"""
    prepared = _CODE_BLOCK.sub(" ", text)
    prepared = _MARKDOWN_LINK.sub(r"\1", prepared)
    prepared = _TABLE_SEPARATOR.sub(" ", prepared)
    prepared = _WHITESPACE.sub(" ", prepared).strip()
    return prepared[:max_chars].rstrip()


class XiaomiTtsProcessor:
    def __init__(
        self,
        *,
        config: XiaomiTtsConfig,
        synthesizer: XiaomiSpeechSynthesizer,
        context: ConversationExtensionContext,
    ) -> None:
        self._config = config
        self._synthesizer = synthesizer
        self._context = context
        self._pending_text: dict[str, str] = {}
        self._jobs: asyncio.Queue[tuple[str, SpeechSynthesisRequest]] = asyncio.Queue(
            maxsize=2
        )
        self._worker_running = False
        self._closed = False

    async def handle_event(self, event: RuntimeEventBase) -> None:
        turn_id = event.envelope.turn_id
        if isinstance(event, AssistantMessageEvent):
            self._pending_text[turn_id] = event.text
            return
        if isinstance(event, TurnCompleted):
            self._handle_turn_completed(event)
            return
        if isinstance(event, (TurnFailed, TurnInterrupted)):
            self._pending_text.pop(turn_id, None)

    def close(self) -> None:
        self._closed = True
        self._pending_text.clear()

    def _handle_turn_completed(self, event: TurnCompleted) -> None:
        text = self._pending_text.pop(event.envelope.turn_id, "")
        if self._closed or event.outcome != "completed":
            return
        text = prepare_speech_text(text, max_chars=self._config.max_text_chars)
        if not text:
            return
        if self._jobs.full():
            try:
                self._jobs.get_nowait()
                self._jobs.task_done()
            except asyncio.QueueEmpty:
                pass
        self._jobs.put_nowait(
            (
                event.envelope.turn_id,
                SpeechSynthesisRequest(
                    text=text,
                    style=self._config.style,
                    voice=self._config.voice,
                ),
            )
        )
        if not self._worker_running:
            self._worker_running = True
            self._context.start_background_task(
                self._run(),
                "xiaomi-tts",
            )

    async def _run(self) -> None:
        try:
            while not self._closed:
                turn_id, request = await self._jobs.get()
                try:
                    audio = await self._synthesizer.synthesize(request)
                    await self._context.publish_output(
                        AudioOutputReady(
                            session_id=self._context.session_id,
                            turn_id=turn_id,
                            source="xiaomi_tts",
                            audio=AudioContent(
                                data=audio.data,
                                media_type=audio.media_type,
                                sample_rate=audio.sample_rate,
                                channels=audio.channels,
                            ),
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — TTS 不影响正文 turn
                    await self._context.publish_output(
                        AudioOutputFailed(
                            session_id=self._context.session_id,
                            turn_id=turn_id,
                            source="xiaomi_tts",
                            message=str(exc),
                            retryable=(
                                exc.retryable
                                if isinstance(exc, SpeechSynthesisError)
                                else False
                            ),
                        )
                    )
                finally:
                    self._jobs.task_done()
        finally:
            self._worker_running = False
