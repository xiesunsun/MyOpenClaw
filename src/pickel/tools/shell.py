from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
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
from typing import Any, Protocol
from uuid import uuid4

from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.sandbox import SandboxPolicy


class ShellStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"  # 前台命令仍在执行（超时后 pending）
    TERMINATED = "terminated"


@dataclass(frozen=True)
class OutputLimits:
    raw_max_chars: int = 2 * 1024 * 1024  # 采集缓冲上限，超过丢中间
    result_max_chars: int = 30_000  # 注入结果上限
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
    environment: str = "local"
    sandboxed: bool = False


class BashOperations(Protocol):
    """``bash`` 工具依赖的最小执行接口。

    模型合同只认识 command 与 timeout；本地 PTY、SSH 或容器实现负责把
    同一合同翻译到各自的执行环境。
    """

    async def exec(
        self,
        *,
        session_id: str,
        workspace_path: Path,
        command: str,
        timeout: float | None = None,
    ) -> ShellExecutionResult: ...

    def close(self, session_id: str) -> None: ...


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
            # macOS 自带 Bash 3.2 会向首次命令输出升级 zsh 的提示；它不是
            # 命令结果，且 Seatbelt 启动稍慢时可能越过启动 drain 窗口。
            shell_env["BASH_SILENCE_DEPRECATION_WARNING"] = "1"
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
            raise RuntimeError("The previous foreground command is still being stopped")

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
                timeout_ms=(
                    self.default_timeout_ms if timeout_ms is None else timeout_ms
                ),
            )
        finally:
            self._running = False

    def _read_until_marker(
        self, marker: str, *, timeout_ms: int
    ) -> ShellExecutionResult:
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
                    _normalize_output(buffer[: marker_match.start()])
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
                # 先返回 pending 结果；LocalBashOperations 随即负责中断前台
                # 进程并恢复 Shell，模型无需管理 Runtime 内部状态。
                stdout, truncated, full_path = self._finalize_output(
                    _normalize_output(buffer)
                )
                return ShellExecutionResult(
                    stdout=stdout,
                    stderr=_normalize_output(err_buffer),
                    status_message=(
                        "Command timed out and is being stopped by the runtime."
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

    def interrupt_foreground(self) -> ShellExecutionResult:
        """超时后停止前台进程；Shell 不可恢复时才丢弃会话。"""
        marker = self._require_pending()
        self.process.signal_foreground(signal.SIGINT)
        result = self._read_until_marker(marker, timeout_ms=2000)
        if result.shell_status is ShellStatus.RUNNING:
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


class ShellSessionManager:
    def __init__(
        self,
        shell_program: str = "/bin/bash",
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.shell_program = shell_program
        self.sandbox = sandbox
        self._sessions: dict[str, PersistentShell] = {}

    def __del__(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)

    def get_or_create(self, session_id: str, workspace_path: Path) -> PersistentShell:
        shell = self._sessions.get(session_id)
        if shell is None:
            workspace = workspace_path.resolve()
            shell = PersistentShell(
                workspace_path=workspace,
                process=PtyShellProcess(
                    shell_program=self.shell_program, sandbox=self.sandbox
                ),
                output_dir=workspace / ".pickel" / "shell-output" / session_id,
            )
            self._sessions[session_id] = shell
        return shell

    def close(self, session_id: str) -> None:
        shell = self._sessions.pop(session_id, None)
        if shell is not None:
            shell.terminate()


class LocalBashOperations:
    """基于现有持久 PTY 的本地 Bash 实现。

    ``ShellSessionManager`` 暂时只作为本实现的内部兼容层；Builtin Tool 与
    Run 不应依赖它的任务管理接口。
    """

    def __init__(self, sessions: ShellSessionManager | None = None) -> None:
        self._sessions = sessions or ShellSessionManager()

    async def exec(
        self,
        *,
        session_id: str,
        workspace_path: Path,
        command: str,
        timeout: float | None = None,
    ) -> ShellExecutionResult:
        shell = self._sessions.get_or_create(session_id, workspace_path)
        timeout_ms = None if timeout is None else max(1, int(timeout * 1000))
        result = await asyncio.to_thread(shell.exec, command, timeout_ms)
        if not result.timed_out:
            return replace(
                result,
                environment="local",
                sandboxed=shell.process.sandboxed,
            )

        stopped = await asyncio.to_thread(shell.interrupt_foreground)
        return ShellExecutionResult(
            stdout=_join_output(result.stdout, stopped.stdout),
            stderr=_join_output(result.stderr, stopped.stderr),
            status_message="Command timed out and the foreground process was stopped.",
            exit_code=124,
            cwd=stopped.cwd,
            shell_status=stopped.shell_status,
            timed_out=True,
            truncated=result.truncated or stopped.truncated,
            full_output_path=stopped.full_output_path or result.full_output_path,
            environment="local",
            sandboxed=shell.process.sandboxed,
        )

    def close(self, session_id: str) -> None:
        self._sessions.close(session_id)


def _join_output(first: str, second: str) -> str:
    if first and second:
        return f"{first}\n{second}"
    return first or second


class BashTool(BaseTool):
    """模型侧唯一的 Bash 工具。"""

    spec = ToolSpec(
        name="bash",
        description=(
            "Execute a Bash command in the agent's working environment. "
            "The shell persists for the current session, so cwd, environment variables, "
            "and background jobs carry over between calls. Use standard Bash syntax for "
            "pipes, redirects, and background jobs. Redirect background output to a file "
            "and use jobs, ps, tail, and kill to manage it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Bash command to execute.",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Maximum foreground execution time in seconds. When exceeded, "
                        "the foreground process is stopped and the shell remains usable."
                    ),
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
                "cwd": {"type": "string"},
                "shell_status": {"type": "string"},
                "timed_out": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "full_output_path": {"type": ["string", "null"]},
                "sandboxed": {"type": "boolean"},
                "environment": {"type": "string"},
            },
            "required": [
                "stdout",
                "stderr",
                "exit_code",
                "cwd",
                "shell_status",
                "timed_out",
                "truncated",
                "full_output_path",
                "sandboxed",
                "environment",
            ],
            "additionalProperties": False,
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        bash = context.services.bash
        if bash is None:
            return ToolExecutionResult(
                content="Bash is not available in this agent environment.",
                is_error=True,
            )

        # 仅作为明显误操作的快速反馈；真正安全边界由执行环境提供。
        reason = _dangerous_command_reason(str(arguments["command"]))
        if reason is not None:
            return ToolExecutionResult(
                content=f"Command blocked ({reason}).",
                is_error=True,
                metadata={"blocked": True, "reason": reason},
            )

        timeout = arguments.get("timeout")
        try:
            result = await bash.exec(
                session_id=context.session_id,
                workspace_path=context.workspace_path,
                command=str(arguments["command"]),
                timeout=float(timeout) if timeout is not None else None,
            )
        except Exception as exc:
            return ToolExecutionResult(
                content=f"Bash execution failed: {exc}",
                is_error=True,
            )

        structured = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "cwd": str(result.cwd),
            "shell_status": result.shell_status.value,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "full_output_path": (
                str(result.full_output_path) if result.full_output_path else None
            ),
            "sandboxed": result.sandboxed,
            "environment": result.environment,
        }
        return ToolExecutionResult(
            content=_format_result_content(result),
            is_error=result.shell_status is ShellStatus.TERMINATED,
            metadata=dict(structured),
            structured_content=structured,
        )


def _format_result_content(result: ShellExecutionResult) -> str:
    parts = [result.stdout] if result.stdout else []
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    if result.status_message:
        parts.append(f"[status] {result.status_message}")
    return "\n".join(parts)


# 仅拦截明显误操作，不维护“危险命令大全”；安全边界由 OS sandbox 提供。
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
