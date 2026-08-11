"""小米 MiMo-V2.5-TTS 自动语音 extension。"""

from __future__ import annotations

import os

from pickel.extensions.xiaomi_tts.config import XiaomiTtsConfig
from pickel.extensions.xiaomi_tts.processor import XiaomiTtsProcessor
from pickel.extensions.xiaomi_tts.synthesizer import XiaomiSpeechSynthesizer
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    AgentRunCompleted,
    AgentRunFailed,
    AgentRunInterrupted,
)


def setup(host) -> None:
    configured = host.config(XiaomiTtsConfig)
    api_key = os.getenv("XIAOMI_API_KEY") or os.getenv("MIMO_API_KEY")
    if configured is None:
        return
    config = configured
    if not config.enabled:
        return
    if not api_key:
        raise RuntimeError("小米自动语音已启用，但未设置 XIAOMI_API_KEY")

    def create_processor(context):
        if context.mode != "interactive":
            return None
        return XiaomiTtsProcessor(
            config=config,
            synthesizer=XiaomiSpeechSynthesizer(
                api_key=api_key,
                timeout_seconds=config.timeout_seconds,
                max_attempts=config.max_attempts,
            ),
            context=context,
        )

    host.add_event_processor(
        event_types=(
            AssistantMessageEvent,
            AgentRunCompleted,
            AgentRunFailed,
            AgentRunInterrupted,
        ),
        factory=create_processor,
    )
