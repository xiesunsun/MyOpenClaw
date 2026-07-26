from __future__ import annotations

import asyncio
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
import threading
import time
from typing import Any
from uuid import uuid4

from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from pickel.tools.sandbox import SandboxPolicy


class ShellStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"          # 前台命令仍在执行（超时后 pending）
    TERMINATED = "terminated"
    TIMED_OUT = "timed_out"
    ERROR = "error"


@dataclass(frozen=True)
class OutputLimits:
    raw_max_chars: int = 2 * 1024 * 1024   # 采集缓冲上限，超过丢中间
    result_max_chars: int = 30_000          # 注入结果上限
    head_chars: int = 20_000
    tail_chars: int = 8_000


@dataclass(frozen=True)
class ShellExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    cwd: Path
    shell_status: ShellStatus
    timed_out: bool = False
    truncated: bool = False
    full_output_path: Path | None = None
    status_message: str = ""


class PtyShellProcess:
    def __init__(
        self,
        shell_program: str = "/bin/bash",
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.shell_program = shell_program
        self.sandbox = sandbox
        self.sandboxed = False
        self._master_fd: int | None = None
        self._stderr_fd: int | None = None
        self._stderr_child_fd: int | None = None
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
            shell_env["PS2"] = ""
            shell_env["PROMPT"] = ""
            shell_env["RPROMPT"] = ""
            # marker 由 PROMPT_COMMAND 发射：bash 每回到顶层提示符必执行，
            # 前台命令死于 SIGINT（bash 会丢弃当前列表剩余部分）时也不例外，
            # 所以中断后依然能拿到 marker + 退出码。marker 为空时不发射，
            # 发射后立即清空，杜绝启动噪声与空行触发的重复 marker。
            shell_env["PROMPT_COMMAND"] = (
                '__pickel_ec=$?; if [ -n "$__pickel_marker" ]; then '
                "printf '%s\\037%s\\037%s\\n' "
                '"$__pickel_marker" "$__pickel_ec" "$PWD"; fi; __pickel_marker='
            )

            if self.sandbox is not None:
                shell_env = self.sandbox.filter_env(shell_env)

            spawn_argv = self._spawn_command()
            if self.sandbox is not None:
                spawn_argv, self.sandboxed = self.sandbox.wrap_command(
                    spawn_argv, workspace=workspace_path
                )

            # 命令 stderr 走独立管道（wrapper 里 2>&N 重定向）。
            # bash 自身的 stderr 必须留在 tty：bash 用 stderr 判定控制终端，
            # 接管道会让 job control 失效（前台命令不再有独立进程组）。
            stderr_read, stderr_write = os.pipe()
            os.set_blocking(stderr_read, False)
            self._process = subprocess.Popen(
                spawn_argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(workspace_path),
                env=shell_env,
                close_fds=True,
                pass_fds=(stderr_write,),
                start_new_session=True,
            )
            self._master_fd = master_fd
            self._stderr_fd = stderr_read
            self._stderr_child_fd = stderr_write
            os.close(stderr_write)
        finally:
            os.close(slave_fd)

        self._drain_startup_output()

    def write(self, data: str) -> None:
        if self._master_fd is None:
            raise RuntimeError("Shell process is not started")
        os.write(self._master_fd, data.encode("utf-8"))

    def read_chunks(self, timeout_ms: int) -> tuple[str, str]:
        """同时读 pty（stdout）与 stderr pipe，返回 (stdout, stderr) 增量。"""
        fds = [fd for fd in (self._master_fd, self._stderr_fd) if fd is not None]
        if not fds:
            return "", ""
        ready, _, _ = select.select(fds, [], [], timeout_ms / 1000)
        out = err = ""
        for fd in ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                continue
            text = chunk.decode("utf-8", errors="replace")
            if fd == self._master_fd:
                out += text
            else:
                err += text
        return out, err

    def foreground_pgid(self) -> int | None:
        """pty 前台进程组；等于 shell 自身（没有前台命令）时返回 None。"""
        if self._master_fd is None or self._process is None:
            return None
        try:
            pgid = os.tcgetpgrp(self._master_fd)
        except OSError:
            return None
        if pgid <= 0 or pgid == self._process.pid:
            return None
        return pgid

    def signal_foreground(self, sig: signal.Signals) -> bool:
        pgid = self.foreground_pgid()
        if pgid is None:
            return False
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False

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
        if self._stderr_fd is not None:
            try:
                os.close(self._stderr_fd)
            except OSError:
                pass
        self._stderr_fd = None
        self._stderr_child_fd = None
        self._process = None

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _drain_startup_output(self) -> None:
        if self._master_fd is None:
            return
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            out, err = self.read_chunks(timeout_ms=10)
            if not out and not err:
                break

    def _spawn_command(self) -> list[str]:
        shell_name = Path(self.shell_program).name
        if "bash" in shell_name:
            # --noediting：pty 上没有人类编辑命令，不需要 readline；
            # 不关的话 bash≥5.1 的 bracketed-paste 会把 \x1b[?2004h/l
            # 写进 pty 流，混进工具结果进而污染发给模型的上下文
            return [self.shell_program, "--noprofile", "--norc", "--noediting", "-s"]
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
        output_dir: Path | None = None,
        limits: OutputLimits | None = None,
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.process = process or PtyShellProcess(sandbox=sandbox)
        self.default_timeout_ms = default_timeout_ms
        self._last_cwd = self.workspace_path
        self._running = False
        self._output_dir = output_dir
        self._limits = limits or OutputLimits()
        self._output_seq = 0
        self._pending_marker: str | None = None

    @property
    def cwd(self) -> Path:
        return self._last_cwd

    @property
    def pending(self) -> bool:
        """前台命令是否仍未结束（超时后可用三件套续处理）。"""
        return self._pending_marker is not None

    def start(self) -> None:
        self.process.spawn(self.workspace_path)

    def is_alive(self) -> bool:
        return self.process.is_alive()

    def terminate(self) -> None:
        self.process.terminate()
        self._pending_marker = None

    def exec(self, command: str, timeout_ms: int | None = None) -> ShellExecutionResult:
        if self._running:
            raise RuntimeError("The shell is already executing a command")
        if self.pending:
            raise RuntimeError(
                "A foreground command is still running; "
                "use shell_wait / shell_stdin / shell_interrupt first"
            )

        self.start()
        if not self.is_alive():
            return ShellExecutionResult(
                stdout="",
                stderr="",
                status_message="Shell is not running.",
                exit_code=1,
                cwd=self._last_cwd,
                shell_status=ShellStatus.TERMINATED,
            )

        marker = f"__MYOPENCLAW_DONE_{uuid4().hex}__"
        wrapped_command = self._build_wrapped_command(command, marker)
        self._running = True
        self._pending_marker = marker
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
        err_buffer = ""
        deadline = time.monotonic() + (timeout_ms / 1000)

        while True:
            if not self.is_alive():
                self._pending_marker = None
                stdout, truncated, full_path = self._finalize_output(
                    _normalize_output(buffer)
                )
                return ShellExecutionResult(
                    stdout=stdout,
                    stderr=_normalize_output(err_buffer),
                    status_message="Shell terminated unexpectedly.",
                    exit_code=1,
                    cwd=self._last_cwd,
                    shell_status=ShellStatus.TERMINATED,
                    truncated=truncated,
                    full_output_path=full_path,
                )

            marker_match = _find_marker(buffer, marker)
            if marker_match is not None:
                self._pending_marker = None
                # 命令已结束；stderr pipe 可能还有残余，短窗口 drain 一次
                _, late_err = self.process.read_chunks(timeout_ms=50)
                err_buffer += late_err
                output, truncated, full_path = self._finalize_output(
                    _normalize_output(buffer[:marker_match.start()])
                )
                exit_code = int(marker_match.group("exit_code"))
                cwd = Path(marker_match.group("cwd"))
                self._last_cwd = cwd
                return ShellExecutionResult(
                    stdout=output,
                    stderr=_normalize_output(err_buffer),
                    exit_code=exit_code,
                    cwd=cwd,
                    shell_status=ShellStatus.READY,
                    truncated=truncated,
                    full_output_path=full_path,
                )

            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                # 超时不杀任何进程：命令继续在前台跑，会话进 pending，
                # agent 可用 shell_wait / shell_stdin / shell_interrupt 续处理
                stdout, truncated, full_path = self._finalize_output(
                    _normalize_output(buffer)
                )
                return ShellExecutionResult(
                    stdout=stdout,
                    stderr=_normalize_output(err_buffer),
                    status_message=(
                        "Command timed out and is still running in the foreground. "
                        "Use shell_wait to keep waiting, shell_stdin to send input, "
                        "or shell_interrupt to stop it."
                    ),
                    exit_code=124,
                    cwd=self._last_cwd,
                    shell_status=ShellStatus.RUNNING,
                    timed_out=True,
                    truncated=truncated,
                    full_output_path=full_path,
                )

            out_chunk, err_chunk = self.process.read_chunks(
                timeout_ms=min(remaining_ms, 100)
            )
            if out_chunk:
                buffer += out_chunk
            if err_chunk:
                err_buffer += err_chunk
            if len(buffer) > self._limits.raw_max_chars:
                keep_head = self._limits.raw_max_chars // 2
                keep_tail = self._limits.raw_max_chars // 4
                buffer = (
                    buffer[:keep_head]
                    + "\n... [raw output dropped] ...\n"
                    + buffer[-keep_tail:]
                )

    def wait_foreground(self, timeout_ms: int | None = None) -> ShellExecutionResult:
        """续等 pending 前台命令，直到 marker 或再次超时。"""
        marker = self._require_pending()
        return self._read_until_marker(
            marker,
            timeout_ms=self.default_timeout_ms if timeout_ms is None else timeout_ms,
        )

    def write_stdin(self, text: str, *, newline: bool = True) -> ShellExecutionResult:
        """向 pending 前台命令写入文本，返回短窗口内的增量输出。"""
        self._require_pending()
        self.process.write(text + ("\n" if newline else ""))
        return self._read_pending_window(window_ms=300)

    def interrupt_foreground(self, *, kill: bool = False) -> ShellExecutionResult:
        """SIGINT（或 SIGKILL）前台进程组；shell 不可恢复时才弃会话。"""
        marker = self._require_pending()
        sig = signal.SIGKILL if kill else signal.SIGINT
        self.process.signal_foreground(sig)
        result = self._read_until_marker(marker, timeout_ms=2000)
        if result.shell_status is ShellStatus.RUNNING and not kill:
            # SIGINT 无效（命令捕获/忽略了），升级 SIGKILL 再试一轮
            self.process.signal_foreground(signal.SIGKILL)
            result = self._read_until_marker(marker, timeout_ms=2000)
        if result.shell_status is ShellStatus.RUNNING:
            # 前台杀不掉且 marker 不出 —— shell 已不可用，弃会话
            self.terminate()
            return ShellExecutionResult(
                stdout=result.stdout,
                stderr=result.stderr,
                status_message="Foreground command could not be stopped; shell terminated.",
                exit_code=137,
                cwd=self._last_cwd,
                shell_status=ShellStatus.TERMINATED,
            )
        return result

    def _require_pending(self) -> str:
        if self._pending_marker is None:
            raise RuntimeError("No foreground command is pending")
        return self._pending_marker

    def _read_pending_window(self, *, window_ms: int) -> ShellExecutionResult:
        """短窗口增量读：可能读到 marker（命令因输入而结束），也可能只有增量输出。"""
        marker = self._require_pending()
        return self._read_until_marker(marker, timeout_ms=window_ms)

    def _finalize_output(self, output: str) -> tuple[str, bool, Path | None]:
        """结果档：超上限截中间保头尾，完整输出落盘给引用。"""
        limits = self._limits
        if len(output) <= limits.result_max_chars:
            return output, False, None
        path: Path | None = None
        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._output_seq += 1
            path = self._output_dir / f"{int(time.time())}-{self._output_seq}.log"
            path.write_text(output, encoding="utf-8")
        omitted = len(output) - limits.head_chars - limits.tail_chars
        ref = f", full output: {path}" if path is not None else ""
        middle = f"\n... [truncated {omitted} chars{ref}] ...\n"
        return (
            output[: limits.head_chars] + middle + output[-limits.tail_chars :],
            True,
            path,
        )

    def _build_wrapped_command(self, command: str, marker: str) -> str:
        # 花括号组：整个复合命令先被 bash 解析完（stdin 队列被消费干净）再执行，
        # 因此命令内部的 read 只会等待新输入，不会把后续行吃掉。
        # 组内先放一个 : 兜住空命令/纯注释（否则 { } 空组是语法错误）。
        # 组级 2>&N 把命令 stderr 送进独立管道（bash 自身 stderr 留在 tty）。
        # marker 不在这里发射——由 PROMPT_COMMAND 发射（见 spawn），
        # 否则前台命令死于 SIGINT 时 bash 丢弃列表剩余部分，marker 永不到达。
        stderr_fd = getattr(self.process, "_stderr_child_fd", None)
        redirect = f" 2>&{stderr_fd}" if stderr_fd is not None else ""
        return f"__pickel_marker='{marker}'; {{ :\n{command}\n}}{redirect}\n"


@dataclass
class ShellSession:
    session_id: str
    workspace_path: Path
    shell: PersistentShell
    created_at: float
    last_used_at: float


class BackgroundTask:
    """独立 pty 上跑的一条后台命令。reader 线程持续采集输出。"""

    def __init__(
        self,
        *,
        task_id: str,
        command: str,
        workspace_path: Path,
        shell_program: str,
        limits: OutputLimits | None = None,
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.task_id = task_id
        self.command = command
        self.started_at = time.time()
        self._limits = limits or OutputLimits()
        self._lock = threading.Lock()
        self._buffer = ""
        self._process = PtyShellProcess(
            shell_program=shell_program, sandbox=sandbox
        )
        self._process.spawn(workspace_path)
        self._process.write(self.command + "\nexit $?\n")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while self._process.is_alive():
            out, err = self._process.read_chunks(timeout_ms=200)
            if out or err:
                self._append(out + err)
        # 进程退出后 drain 残余
        out, err = self._process.read_chunks(timeout_ms=100)
        if out or err:
            self._append(out + err)

    def _append(self, text: str) -> None:
        with self._lock:
            self._buffer += text
            if len(self._buffer) > self._limits.raw_max_chars:
                keep = self._limits.raw_max_chars // 2
                self._buffer = (
                    self._buffer[:keep] + "\n... [dropped] ...\n"
                    + self._buffer[-keep // 2 :]
                )

    def read_output(self, since: int = 0) -> tuple[str, int]:
        with self._lock:
            text = _normalize_output(self._buffer)
        return text[since:], len(text)

    def status(self) -> str:
        return "running" if self._process.is_alive() else "exited"

    @property
    def exit_code(self) -> int | None:
        proc = self._process._process
        return None if proc is None else proc.poll()

    def kill(self) -> None:
        self._process.terminate()


class ShellSessionManager:
    def __init__(
        self,
        shell_program: str = "/bin/bash",
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.shell_program = shell_program
        self.sandbox = sandbox
        self._sessions: dict[str, ShellSession] = {}
        self._background: dict[str, dict[str, BackgroundTask]] = {}

    def __del__(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)

    def start_background(
        self, session_id: str, workspace_path: Path, command: str
    ) -> BackgroundTask:
        task = BackgroundTask(
            task_id=uuid4().hex[:8],
            command=command,
            workspace_path=workspace_path.resolve(),
            shell_program=self.shell_program,
            sandbox=self.sandbox,
        )
        self._background.setdefault(session_id, {})[task.task_id] = task
        return task

    def background_tasks(self, session_id: str) -> list[BackgroundTask]:
        return list(self._background.get(session_id, {}).values())

    def get_background(self, session_id: str, task_id: str) -> BackgroundTask | None:
        return self._background.get(session_id, {}).get(task_id)

    def kill_background(self, session_id: str, task_id: str) -> bool:
        task = self.get_background(session_id, task_id)
        if task is None:
            return False
        task.kill()
        return True

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
                    process=PtyShellProcess(

                        shell_program=self.shell_program, sandbox=self.sandbox

                    ),
                    output_dir=workspace_path.resolve()
                    / ".pickel" / "shell-output" / session_id,
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
                process=PtyShellProcess(

                    shell_program=self.shell_program, sandbox=self.sandbox

                ),
                output_dir=workspace_path.resolve()
                / ".pickel" / "shell-output" / session_id,
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
        for task in self._background.pop(session_id, {}).values():
            task.kill()


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
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run in the background; returns a task_id to poll with shell_output."
                    ),
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

        reason = _dangerous_command_reason(str(arguments["command"]))
        if reason is not None:
            return ToolExecutionResult(
                content=(
                    f"Command blocked ({reason}). "
                    "If this is genuinely intended, ask the user to run it manually."
                ),
                is_error=True,
                metadata={"blocked": True, "reason": reason},
            )

        if arguments.get("background"):
            task = manager.start_background(
                context.session_id, context.workspace_path, str(arguments["command"])
            )
            return ToolExecutionResult(
                content=f"Background task started: {task.task_id}",
                metadata={"task_id": task.task_id, "background": True},
            )

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

        try:
            result = await asyncio.to_thread(
                session.shell.exec,
                str(arguments["command"]),
                int(timeout_ms) if timeout_ms is not None else None,
            )
        except RuntimeError as exc:
            return ToolExecutionResult(content=str(exc), is_error=True)
        return ToolExecutionResult(
            content=_format_result_content(result),
            is_error=(
                result.exit_code != 0
                or result.shell_status not in (ShellStatus.READY, ShellStatus.RUNNING)
            ),
            metadata=_result_metadata(result, created_new_shell=created_new_shell),
        )


def _format_result_content(result: ShellExecutionResult) -> str:
    parts = [result.stdout] if result.stdout else []
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    if result.status_message:
        parts.append(f"[status] {result.status_message}")
    return "\n".join(parts)


def _result_metadata(
    result: ShellExecutionResult, **extra: Any
) -> dict[str, Any]:
    return {
        "cwd": str(result.cwd),
        "exit_code": result.exit_code,
        "shell_status": result.shell_status,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "full_output_path": (
            str(result.full_output_path) if result.full_output_path else None
        ),
        "stderr_chars": len(result.stderr),
        **extra,
    }


def _foreground_result(result: ShellExecutionResult) -> ToolExecutionResult:
    # 三件套的 is_error 只看 shell 是否被弃：非零退出码（如 SIGINT 的 130）
    # 是被请求动作的正常结果，不是工具错误
    return ToolExecutionResult(
        content=_format_result_content(result) or "(no new output)",
        is_error=result.shell_status is ShellStatus.TERMINATED,
        metadata=_result_metadata(result),
    )


def _require_pending_shell(
    manager: ShellSessionManager, session_id: str
) -> ShellSession | None:
    session = manager.get(session_id)
    if session is None or not session.shell.pending:
        return None
    return session


def _no_pending_result() -> ToolExecutionResult:
    return ToolExecutionResult(
        content="No foreground command is pending.", is_error=True
    )


class ShellWaitTool(BaseTool):
    spec = ToolSpec(
        name="shell_wait",
        description=(
            "Keep waiting for the foreground command that previously timed out in shell_exec. "
            "Returns new output since the last call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How long to wait this time, in milliseconds.",
                },
            },
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        session = _require_pending_shell(manager, context.session_id)
        if session is None:
            return _no_pending_result()
        timeout_ms = arguments.get("timeout_ms")
        result = await asyncio.to_thread(
            session.shell.wait_foreground,
            int(timeout_ms) if timeout_ms is not None else None,
        )
        return _foreground_result(result)


class ShellStdinTool(BaseTool):
    spec = ToolSpec(
        name="shell_stdin",
        description=(
            "Send text to the stdin of the foreground command still running in the session shell "
            "(e.g. to answer an interactive prompt). Returns any output produced shortly after."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to write to the foreground command's stdin.",
                },
                "newline": {
                    "type": "boolean",
                    "description": "Append a trailing newline (submit the input). Default true.",
                },
            },
            "required": ["text"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        session = _require_pending_shell(manager, context.session_id)
        if session is None:
            return _no_pending_result()
        result = await asyncio.to_thread(
            lambda: session.shell.write_stdin(
                str(arguments["text"]),
                newline=bool(arguments.get("newline", True)),
            )
        )
        return _foreground_result(result)


class ShellInterruptTool(BaseTool):
    spec = ToolSpec(
        name="shell_interrupt",
        description=(
            "Stop the foreground command still running in the session shell. "
            "Sends SIGINT first (escalating to SIGKILL if needed); set kill=true to SIGKILL directly. "
            "The shell session itself survives and stays usable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kill": {
                    "type": "boolean",
                    "description": "Send SIGKILL immediately instead of SIGINT. Default false.",
                },
            },
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        session = _require_pending_shell(manager, context.session_id)
        if session is None:
            return _no_pending_result()
        result = await asyncio.to_thread(
            lambda: session.shell.interrupt_foreground(
                kill=bool(arguments.get("kill", False))
            )
        )
        return _foreground_result(result)


def _unknown_task_result(
    manager: ShellSessionManager, session_id: str, task_id: str
) -> ToolExecutionResult:
    known = [task.task_id for task in manager.background_tasks(session_id)]
    listing = ", ".join(known) if known else "(none)"
    return ToolExecutionResult(
        content=f"Unknown task_id: {task_id}. Known tasks: {listing}",
        is_error=True,
    )


class ShellTasksTool(BaseTool):
    spec = ToolSpec(
        name="shell_tasks",
        description="List background tasks started with shell_exec background=true in this session.",
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
        tasks = manager.background_tasks(context.session_id)
        if not tasks:
            return ToolExecutionResult(content="No background tasks.")
        lines = []
        now = time.time()
        for task in tasks:
            runtime = int(now - task.started_at)
            command = task.command.replace("\n", " ")[:60]
            lines.append(f"{task.task_id}  {task.status()}  {runtime}s  {command}")
        return ToolExecutionResult(
            content="\n".join(lines),
            metadata={"task_ids": [task.task_id for task in tasks]},
        )


class ShellOutputTool(BaseTool):
    spec = ToolSpec(
        name="shell_output",
        description=(
            "Read output from a background task. Pass the `since` offset from the previous "
            "call's metadata to read only new output."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Background task id returned by shell_exec.",
                },
                "since": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Read from this offset (metadata.next_since of the previous call).",
                },
            },
            "required": ["task_id"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        task_id = str(arguments["task_id"])
        task = manager.get_background(context.session_id, task_id)
        if task is None:
            return _unknown_task_result(manager, context.session_id, task_id)
        text, next_since = task.read_output(since=int(arguments.get("since", 0)))
        return ToolExecutionResult(
            content=text,
            metadata={
                "task_id": task.task_id,
                "next_since": next_since,
                "status": task.status(),
                "exit_code": task.exit_code,
            },
        )


class ShellKillTool(BaseTool):
    spec = ToolSpec(
        name="shell_kill",
        description="Terminate a background task started with shell_exec background=true.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Background task id returned by shell_exec.",
                },
            },
            "required": ["task_id"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        manager = _require_shell_manager(context)
        task_id = str(arguments["task_id"])
        task = manager.get_background(context.session_id, task_id)
        if task is None:
            return _unknown_task_result(manager, context.session_id, task_id)
        await asyncio.to_thread(task.kill)
        return ToolExecutionResult(
            content=f"Task {task.task_id} killed.",
            metadata={
                "task_id": task.task_id,
                "status": task.status(),
                "exit_code": task.exit_code,
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


# 危险命令静态拦截：挡「明显自杀」，不做 shell 解析级对抗（真防线在 S2 sandbox）
_DANGEROUS_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+(?:-\w+\s+)*-\w*[rR]\w*\s+"
            r"(?:-\w+\s+)*(/|~|\$HOME)(?:/?\*)?\s*(?:$|[;&|])"
        ),
        "recursive delete of / or home",
    ),
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?mkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "raw write to block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (
        re.compile(
            r"(?:^|[;&|]\s*)(?:sudo\s+)?ch(?:mod|own)\s+(?:-\w+\s+)*-R\s+\S+\s+/\s*(?:$|[;&|])"
        ),
        "recursive chmod/chown on /",
    ),
]


def _dangerous_command_reason(command: str) -> str | None:
    # 引号内内容不参与匹配（echo 'rm -rf /' 不应命中）
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "''", command)
    for pattern, reason in _DANGEROUS_RULES:
        if pattern.search(stripped):
            return reason
    return None


def _require_shell_manager(context: ToolExecutionContext) -> ShellSessionManager:
    manager = context.services.shell_sessions
    if manager is None:
        raise RuntimeError("A shell session manager is required for shell tools")
    return manager


def _find_marker(buffer: str, marker: str) -> re.Match[str] | None:
    pattern = re.compile(
        rf"{re.escape(marker)}\x1f(?P<exit_code>-?\d+)\x1f(?P<cwd>[^\r\n]*)"
    )
    return pattern.search(buffer)


# CSI（\x1b[...字母）与 OSC（\x1b]...BEL 或 \x1b]...ST）序列
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _normalize_output(output: str) -> str:
    normalized = _ANSI_RE.sub("", output)
    normalized = normalized.replace("\r", "")
    return normalized.rstrip("\n")
