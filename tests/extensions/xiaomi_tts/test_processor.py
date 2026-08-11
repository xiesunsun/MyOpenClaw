"""自动语音事件归并与音频输出。"""

import asyncio
import unittest

from pickel.shared.conversation_output import AudioOutputFailed, AudioOutputReady
from pickel.extensions.xiaomi_tts.config import XiaomiTtsConfig
from pickel.extensions.xiaomi_tts.processor import (
    XiaomiTtsProcessor,
    prepare_speech_text,
)
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions.xiaomi_tts.synthesizer import (
    SpeechSynthesisError,
    SynthesizedAudio,
)
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    AgentRunCompleted,
    AgentRunInterrupted,
)
from pickel.shared.event_envelope import EventEnvelope


class _Synthesizer:
    def __init__(self, error=None) -> None:
        self.requests = []
        self.error = error

    async def synthesize(self, request) -> bytes:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SynthesizedAudio(data=b"wav", media_type="audio/wav")


def _envelope(operation_id: str = "turn-1") -> EventEnvelope:
    return EventEnvelope(session_id="session-1", operation_id=operation_id)


class PrepareSpeechTextTests(unittest.TestCase):
    def test_removes_code_and_link_url(self) -> None:
        text = "结果见 [文档](https://example.com)。\n```python\nprint('x')\n```"
        self.assertEqual(
            "结果见 文档。",
            prepare_speech_text(text, max_chars=100),
        )


class XiaomiTtsProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.outputs = []
        self.output_ready = asyncio.Event()
        self.tasks = []

        async def publish(output) -> None:
            self.outputs.append(output)
            self.output_ready.set()

        def start(coroutine, name) -> None:
            self.tasks.append(asyncio.create_task(coroutine, name=name))

        self.context = ConversationExtensionContext(
            agent_id="Pickle",
            session_id="session-1",
            mode="interactive",
            publish_output=publish,
            start_background_task=start,
        )
        self.synthesizer = _Synthesizer()
        self.processor = XiaomiTtsProcessor(
            config=XiaomiTtsConfig(style="自然", voice="冰糖"),
            synthesizer=self.synthesizer,  # type: ignore[arg-type]
            context=self.context,
        )

    async def asyncTearDown(self) -> None:
        self.processor.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_publishes_latest_assistant_message_after_agent_run_completed(
        self,
    ) -> None:
        await self.processor.handle_event(
            AssistantMessageEvent(envelope=_envelope(), text="第一版")
        )
        await self.processor.handle_event(
            AssistantMessageEvent(envelope=_envelope(), text="最终回答")
        )
        await self.processor.handle_event(AgentRunCompleted(envelope=_envelope()))

        await asyncio.wait_for(self.output_ready.wait(), timeout=1)

        self.assertEqual("最终回答", self.synthesizer.requests[0].text)
        self.assertEqual("自然", self.synthesizer.requests[0].style)
        self.assertIsInstance(self.outputs[0], AudioOutputReady)
        self.assertEqual(b"wav", self.outputs[0].audio.data)

    async def test_interrupted_turn_does_not_publish_audio(self) -> None:
        await self.processor.handle_event(
            AssistantMessageEvent(envelope=_envelope(), text="未完成")
        )
        await self.processor.handle_event(AgentRunInterrupted(envelope=_envelope()))
        await self.processor.handle_event(AgentRunCompleted(envelope=_envelope()))
        await asyncio.sleep(0)

        self.assertEqual([], self.synthesizer.requests)
        self.assertEqual([], self.outputs)

    async def test_publishes_structured_failure(self) -> None:
        self.processor._synthesizer = _Synthesizer(
            SpeechSynthesisError("临时失败", retryable=True)
        )  # type: ignore[assignment]
        await self.processor.handle_event(
            AssistantMessageEvent(envelope=_envelope(), text="最终回答")
        )
        await self.processor.handle_event(AgentRunCompleted(envelope=_envelope()))

        await asyncio.wait_for(self.output_ready.wait(), timeout=1)

        self.assertIsInstance(self.outputs[0], AudioOutputFailed)
        self.assertTrue(self.outputs[0].retryable)
        self.assertEqual("临时失败", self.outputs[0].message)


if __name__ == "__main__":
    unittest.main()
