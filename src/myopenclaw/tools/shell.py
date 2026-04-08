from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import termios
import time
from typing import Any
from uuid import uuid4

from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


class ShellStatus(StrEnum):
    READY = "ready"
    TERMINATED = "terminated"
    TIMED_OUT = "timed_out"
    ERROR = "error"


@dataclass(frozen=True)
class ShellExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    cwd: Path
    shell_status: ShellStatus
    timed_out: bool = False
    truncated: bool = False


class PtyShellProcess:
    def __init__(self, shell_program: str = "/bin/bash") -> None:
        self.shell_program = shell_program
        self._master_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None

    def spawn(self, workspace_path: Path, env: dict[str, str] | None = None) -> None:
        if self.is_alive():
            return

        master_fd, slave_fd = pty.openpty()
        try:
            attributes = termios.tcgetattr(slave_fd)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)

            shell_env = dict(os.environ)
            if env:
                shell_env.update(env)
            shell_env.setdefault("TERM", "dumb")
            shell_env["PS1"] = ""
            shell_env["PROMPT"] = ""
            shell_env["RPROMPT"] = ""

            self._process = subprocess.Popen(
                self._spawn_command(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(workspace_path),
                env=shell_env,
                close_fds=True,
                start_new_session=True,
            )
            self._master_fd = master_fd
        finally:
            os.close(slave_fd)

        self._drain_startup_output()

    def write(self, data: str) -> None:
        if self._master_fd is None:
            raise RuntimeError("Shell process is not started")
        os.write(self._master_fd, data.encode("utf-8"))

    def read_chunk(self, timeout_ms: int) -> str:
        if self._master_fd is None:
            return ""
        ready, _, _ = select.select([self._master_fd], [], [], timeout_ms / 1000)
        if not ready:
            return ""
        try:
            chunk = os.read(self._master_fd, 4096)
        except OSError:
            return ""
        return chunk.decode("utf-8", errors="replace")

    def interrupt(self) -> None:
        if not self.is_alive() or self._process is None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def terminate(self) -> None:
        if self._process is None:
            return

        if self.is_alive():
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._process.wait(timeout=1)

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._process = None

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _drain_startup_output(self) -> None:
        if self._master_fd is None:
            return
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            chunk = self.read_chunk(timeout_ms=10)
            if not chunk:
                break

    def _spawn_command(self) -> list[str]:
        shell_name = Path(self.shell_program).name
        if "bash" in shell_name:
            return [self.shell_program, "--noprofile", "--norc", "-s"]
        if "zsh" in shell_name:
            return [self.shell_program, "-f", "-s"]
        return [self.shell_program]


class PersistentShell:
    def __init__(
        self,
        *,
        workspace_path: Path,
        process: PtyShellProcess | None = None,
        default_timeout_ms: int = 120000,
        max_output_chars: int = 4000,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.process = process or PtyShellProcess()
        self.default_timeout_ms = default_timeout_ms
        self.max_output_chars = max_output_chars
        self._last_cwd = self.workspace_path
        self._running = False

    @property
    def cwd(self) -> Path:
        return self._last_cwd

    def start(self) -> None:
        self.process.spawn(self.workspace_path)

    def is_alive(self) -> bool:
        return self.process.is_alive()

    def terminate(self) -> None:
        self.process.terminate()

    def exec(self, command: str, timeout_ms: int | None = None) -> ShellExecutionResult:
        if self._running:
            raise RuntimeError("The shell is already executing a command")

        self.start()
        if not self.is_alive():
            return ShellExecutionResult(
                stdout="",
                stderr="Shell is not running.",
                exit_code=1,
                cwd=self._last_cwd,
                shell_status=ShellStatus.TERMINATED,
            )

        marker = f"__MYOPENCLAW_DONE_{uuid4().hex}__"
        wrapped_command = self._build_wrapped_command(command, marker)
        self._running = True
        try:
            self.process.write(wrapped_command)
            return self._read_until_marker(
                marker,
                timeout_ms=self.default_timeout_ms if timeout_ms is None else timeout_ms,
            )
        finally:
            self._running = False

    def _read_until_marker(self, marker: str, *, timeout_ms: int) -> ShellExecutionResult:
        buffer = ""
        deadline = time.monotonic() + (timeout_ms / 1000)

        while True:
            if not self.is_alive():
                output, truncated = _truncate_output(_normalize_output(buffer), self.max_output_chars)
                return ShellExecutionResult(
                    stdout=output,
                    stderr="Shell terminated unexpectedly.",
                    exit_code=1,
                    cwd=self._last_cwd,
                    shell_status=ShellStatus.TERMINATED,
                    truncated=truncated,
                )

            marker_match = _find_marker(buffer, marker)
            if marker_match is not None:
                output = _normalize_output(buffer[:marker_match.start()])
                output, truncated = _truncate_output(output, self.max_output_chars)
                exit_code = int(marker_match.group("exit_code"))
                cwd = Path(marker_match.group("cwd"))
                self._last_cwd = cwd
                return ShellExecutionResult(
                    stdout=output,
                    stderr="",
                    exit_code=exit_code,
                    cwd=cwd,
                    shell_status=ShellStatus.READY,
                    truncated=truncated,
                )

            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                self.process.interrupt()
                output, truncated = _truncate_output(_normalize_output(buffer), self.max_output_chars)
                self.terminate()
                return ShellExecutionResult(
                    stdout=output,
                    stderr="Shell command timed out.",
                    exit_code=124,
                    cwd=self._last_cwd,
                    shell_status=ShellStatus.TIMED_OUT,
                    timed_out=True,
                    truncated=truncated,
                )

            chunk = self.process.read_chunk(timeout_ms=min(remaining_ms, 100))
            if chunk:
                buffer += chunk

    @staticmethod
    def _build_wrapped_command(command: str, marker: str) -> str:
        return (
            f"{command}\n"
            "__myopenclaw_exit_code=$?\n"
            f"printf '%s\\037%s\\037%s\\n' '{marker}' \"$__myopenclaw_exit_code\" \"$PWD\"\n"
        )


@dataclass
class ShellSession:
    session_id: str
    workspace_path: Path
    shell: PersistentShell
    created_at: float
    last_used_at: float


class ShellSessionManager:
    def __init__(self, shell_program: str = "/bin/bash") -> None:
        self.shell_program = shell_program
        self._sessions: dict[str, ShellSession] = {}

    def __del__(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)

    def get(self, session_id: str) -> ShellSession | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str, workspace_path: Path) -> ShellSession:
        session = self._sessions.get(session_id)
        if session is None:
            now = time.time()
            session = ShellSession(
                session_id=session_id,
                workspace_path=workspace_path.resolve(),
                shell=PersistentShell(
                    workspace_path=workspace_path.resolve(),
                    process=PtyShellProcess(shell_program=self.shell_program),
                ),
                created_at=now,
                last_used_at=now,
            )
            self._sessions[session_id] = session
        session.last_used_at = time.time()
        return session

    def restart(self, session_id: str, workspace_path: Path) -> ShellSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            existing.shell.terminate()
        session = ShellSession(
            session_id=session_id,
            workspace_path=workspace_path.resolve(),
            shell=PersistentShell(
                workspace_path=workspace_path.resolve(),
                process=PtyShellProcess(shell_program=self.shell_program),
            ),
            created_at=time.time(),
            last_used_at=time.time(),
        )
        session.shell.start()
        self._sessions[session_id] = session
        return session

    def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.shell.terminate()


class ShellExecTool(BaseTool):
    spec = ToolSpec(
        name="shell_exec",
        description=(
            "Execute a command inside the current session shell. "
            "The shell is persistent for the duration of the conversation session and starts in the workspace directory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the current persistent shell.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Optional timeout override for this command in milliseconds.",
                    "minimum": 1,
                },
            },
            "required": ["command"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        created_new_shell = manager.get(context.session_id) is None
        session = manager.get_or_create(context.session_id, context.workspace_path)

        if not created_new_shell and not session.shell.is_alive():
            return ToolExecutionResult(
                content="Shell is not running. Call shell_restart to create a fresh shell.",
                is_error=True,
                metadata={
                    "cwd": str(session.shell.cwd),
                    "exit_code": 1,
                    "shell_status": ShellStatus.TERMINATED,
                    "timed_out": False,
                    "truncated": False,
                    "created_new_shell": False,
                },
            )

        timeout_ms = arguments.get("timeout_ms")
        if timeout_ms is not None and int(timeout_ms) <= 0:
            return ToolExecutionResult(
                content="timeout_ms must be a positive integer.",
                is_error=True,
                metadata={
                    "cwd": str(session.shell.cwd),
                    "exit_code": 1,
                    "shell_status": ShellStatus.ERROR,
                    "timed_out": False,
                    "truncated": False,
                    "created_new_shell": created_new_shell,
                },
            )

        result = session.shell.exec(
            str(arguments["command"]),
            timeout_ms=int(timeout_ms) if timeout_ms is not None else None,
        )
        content = result.stdout or result.stderr
        return ToolExecutionResult(
            content=content,
            is_error=result.exit_code != 0 or result.shell_status != ShellStatus.READY,
            metadata={
                "cwd": str(result.cwd),
                "exit_code": result.exit_code,
                "shell_status": result.shell_status,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "created_new_shell": created_new_shell,
            },
        )


class ShellRestartTool(BaseTool):
    spec = ToolSpec(
        name="shell_restart",
        description="Restart the current session shell and reset it to the workspace root.",
        input_schema={
            "type": "object",
            "properties": {},
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        session = manager.restart(context.session_id, context.workspace_path)
        return ToolExecutionResult(
            content=f"Shell restarted at {session.workspace_path}",
            metadata={
                "cwd": str(session.workspace_path),
                "shell_status": ShellStatus.READY,
                "restarted": True,
            },
        )


class ShellCloseTool(BaseTool):
    spec = ToolSpec(
        name="shell_close",
        description="Close the current session shell and release its resources.",
        input_schema={
            "type": "object",
            "properties": {},
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        manager.close(context.session_id)
        return ToolExecutionResult(
            content="Shell closed",
            metadata={
                "shell_status": ShellStatus.TERMINATED,
                "closed": True,
            },
        )


def _require_shell_manager(context: ToolExecutionContext) -> ShellSessionManager:
    manager = context.shell_session_manager
    if manager is None:
        raise RuntimeError("A shell session manager is required for shell tools")
    return manager


def _find_marker(buffer: str, marker: str) -> re.Match[str] | None:
    pattern = re.compile(
        rf"{re.escape(marker)}\x1f(?P<exit_code>-?\d+)\x1f(?P<cwd>[^\r\n]*)"
    )
    return pattern.search(buffer)


def _normalize_output(output: str) -> str:
    normalized = output.replace("\r", "")
    return normalized.rstrip("\n")


def _truncate_output(output: str, max_output_chars: int) -> tuple[str, bool]:
    if len(output) <= max_output_chars:
        return output, False
    return output[:max_output_chars], True
