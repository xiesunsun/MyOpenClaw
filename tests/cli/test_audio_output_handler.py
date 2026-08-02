"""CLI 音频输出处理。"""

import unittest

from pickel.cli.audio_output_handler import CliAudioOutputHandler
from pickel.shared.conversation_output import (
    AudioContent,
    AudioOutputFailed,
    AudioOutputReady,
)


class _Player:
    def __init__(self) -> None:
        self.played = []
        self.stops = 0

    async def play(self, audio) -> None:
        self.played.append(audio)

    def stop(self) -> None:
        self.stops += 1


class CliAudioOutputHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plays_ready_audio(self) -> None:
        player = _Player()
        errors = []
        handler = CliAudioOutputHandler(player=player, render_error=errors.append)
        audio = AudioContent(data=b"wav", media_type="audio/wav")

        await handler.handle_output(AudioOutputReady(audio=audio))

        self.assertEqual([audio], player.played)
        self.assertEqual(1, player.stops)
        self.assertEqual([], errors)

    async def test_renders_generation_failure_without_traceback(self) -> None:
        player = _Player()
        errors = []
        handler = CliAudioOutputHandler(player=player, render_error=errors.append)

        await handler.handle_output(AudioOutputFailed(message="上游暂时不可用"))

        self.assertEqual(["自动语音生成失败: 上游暂时不可用"], errors)
        self.assertEqual([], player.played)
