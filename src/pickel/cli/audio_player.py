"""CLI 使用的本地音频播放器。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
from typing import Protocol

from pickel.shared.conversation_output import AudioContent


class AudioPlayer(Protocol):
    async def play(self, audio: AudioContent) -> None: ...

    def stop(self) -> None: ...


class MacAudioPlayer:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None

    async def play(self, audio: AudioContent) -> None:
        executable = shutil.which("afplay")
        if executable is None:
            raise RuntimeError("未找到 macOS 音频播放器 afplay")
        if audio.media_type != "audio/wav":
            raise ValueError(f"CLI 暂不支持音频格式: {audio.media_type}")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as file:
            file.write(audio.data)
            path = Path(file.name)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._process = process
            await process.wait()
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            if self._process is process:
                self._process = None
            path.unlink(missing_ok=True)

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
