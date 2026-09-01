"""把会话音频输出映射成本地 CLI 副作用。"""

from __future__ import annotations

from collections.abc import Callable

from pickel.shared.conversation_output import (
    AudioOutputFailed,
    AudioOutputReady,
    ConversationOutputBase,
)
from pickel.cli.audio_player import AudioPlayer


class CliAudioOutputHandler:
    def __init__(
        self,
        *,
        player: AudioPlayer,
        render_error: Callable[[str], None],
    ) -> None:
        self._player = player
        self._render_error = render_error

    async def handle_output(self, output: ConversationOutputBase) -> None:
        if isinstance(output, AudioOutputFailed):
            self._render_error(f"自动语音生成失败: {output.message}")
            return
        if not isinstance(output, AudioOutputReady):
            return
        try:
            self._player.stop()
            await self._player.play(output.audio)
        except Exception as exc:  # noqa: BLE001 — 播放失败不影响会话
            self._render_error(f"自动语音播放失败: {exc}")

    def close(self) -> None:
        self._player.stop()
