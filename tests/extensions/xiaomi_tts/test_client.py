"""小米 TTS HTTP 请求与响应解析。"""

import base64
import json
import unittest

import httpx

from pickel.extensions.xiaomi_tts.synthesizer import (
    SpeechSynthesisRequest,
    XiaomiResponseError,
    XiaomiSpeechSynthesizer,
)


class XiaomiSpeechSynthesizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_style_text_and_voice(self) -> None:
        received = {}

        async def handle(request: httpx.Request) -> httpx.Response:
            received["request"] = request
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "audio": {
                                    "data": base64.b64encode(b"wav").decode("ascii")
                                }
                            }
                        }
                    ]
                },
            )

        synthesizer = XiaomiSpeechSynthesizer(
            api_key="secret",
            timeout_seconds=10,
            transport=httpx.MockTransport(handle),
        )

        audio = await synthesizer.synthesize(
            SpeechSynthesisRequest(text="完成了", style="轻松", voice="冰糖")
        )

        request = received["request"]
        self.assertEqual(b"wav", audio.data)
        self.assertEqual("audio/wav", audio.media_type)
        self.assertEqual("secret", request.headers["api-key"])
        payload = json.loads(request.content)
        self.assertEqual("mimo-v2.5-tts", payload["model"])
        self.assertEqual(
            [
                {"role": "user", "content": "轻松"},
                {"role": "assistant", "content": "完成了"},
            ],
            payload["messages"],
        )
        self.assertEqual(
            {"format": "wav", "voice": "冰糖"},
            payload["audio"],
        )

    async def test_rejects_response_without_audio(self) -> None:
        synthesizer = XiaomiSpeechSynthesizer(
            api_key="secret",
            timeout_seconds=10,
            max_attempts=1,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": []})
            ),
        )

        with self.assertRaisesRegex(XiaomiResponseError, "缺少音频数据"):
            await synthesizer.synthesize(
                SpeechSynthesisRequest(text="完成了", style="", voice="冰糖")
            )

    async def test_retries_response_without_audio_once(self) -> None:
        calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": "request-1",
                        "choices": [{"finish_reason": "stop", "message": {}}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "audio": {
                                    "data": base64.b64encode(b"wav").decode("ascii")
                                }
                            }
                        }
                    ]
                },
            )

        synthesizer = XiaomiSpeechSynthesizer(
            api_key="secret",
            timeout_seconds=10,
            transport=httpx.MockTransport(handle),
        )

        audio = await synthesizer.synthesize(
            SpeechSynthesisRequest(text="完成了", style="", voice="冰糖")
        )

        self.assertEqual(b"wav", audio.data)
        self.assertEqual(2, calls)


if __name__ == "__main__":
    unittest.main()
