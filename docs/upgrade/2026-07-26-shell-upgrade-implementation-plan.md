# S1 Shell 升级 实施计划

> **For agentic workers:** 按任务顺序实现，步骤用 checkbox 跟踪。设计依据：`docs/upgrade/2026-07-26-shell-upgrade-design.md`。前置：无（bracketed-paste 已在 `ec78d4a` 修复）。

**Goal:** 给 agent 一个不丢会话、输出可控、可交互、可后台的 shell；六个新工具。

**Architecture:** 全部落在 `tools/shell.py` 工具层内。`PersistentShell` 增 pending 状态与超时不杀语义；`PtyShellProcess` 增 stderr pipe 与前台进程组信号；`ShellSessionManager` 增后台任务记账。总线/Run/ToolServices 不动。

**Tech Stack:** pty + select + threading（后台 reader）+ asyncio.to_thread（解阻塞）。

## Global Constraints

- 测试命令：`uv run --with pytest --with pytest-asyncio pytest`（pytest 非项目依赖）。
- 基线失败 12 例，全部缺 Gemini API key：`3 tests/app/test_assembly.py + 1 tests/cli/test_chat_loop.py + 7 tests/providers/test_gemini.py + 1 tests/providers/test_model_context_generate.py`。每个任务结束核对分布不变：`pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c`
- TDD：先写失败测试 → 确认失败 → 最小实现 → 通过 → commit。
- `tests/tools/test_shell.py` 用 `unittest.IsolatedAsyncioTestCase`，新用例保持同风格；纯同步逻辑用 `unittest.TestCase`。
- **语义精化（相对设计稿 §3.2/§3.4 的歧义，以本计划为准）**：`shell_exec` 超时**不杀任何进程**，返回部分输出 + `shell_status=RUNNING`，会话进 pending；SIGINT→SIGKILL 阶梯移入 `shell_interrupt`。Task 9 把设计稿改齐。

---

### Task 1: ANSI 剥离

**Files:**
- Modify: `src/pickel/tools/shell.py`（`_normalize_output`）
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces: `_ANSI_RE`（模块级）；`_normalize_output` 行为变化（剥 CSI/OSC）

- [ ] **Step 1: 写失败测试**

`tests/tools/test_shell.py` 追加（文件顶部已有 `from pickel.tools.shell import ...`，按需补 import）：

```python
class NormalizeOutputTests(unittest.TestCase):
    def test_strips_csi_color_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b[01;34mdir\x1b[0m\nplain"
        self.assertEqual("dir\nplain", _normalize_output(raw))

    def test_strips_osc_title_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b]0;window-title\x07hello"
        self.assertEqual("hello", _normalize_output(raw))

    def test_strips_private_mode_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b[?25lhello\x1b[?25h"
        self.assertEqual("hello", _normalize_output(raw))

    def test_plain_text_untouched(self) -> None:
        from pickel.tools.shell import _normalize_output

        self.assertEqual("a\nb", _normalize_output("a\r\nb\r\n"))
```

- [ ] **Step 2: 确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_shell.py -q -k Normalize
```

Expected: FAIL（CSI/OSC 未剥离）。

- [ ] **Step 3: 实现**

`src/pickel/tools/shell.py`，`_normalize_output` 上方加模块级正则、函数改为：

```python
# CSI（\x1b[...字母）与 OSC（\x1b]...BEL 或 \x1b]...ST）序列
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _normalize_output(output: str) -> str:
    normalized = _ANSI_RE.sub("", output)
    normalized = normalized.replace("\r", "")
    return normalized.rstrip("\n")
```

- [ ] **Step 4: 通过 + 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "feat(shell): 输出剥离 ANSI CSI/OSC 序列"`

---

### Task 2: 输出三档上限与落盘

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces:
  - `OutputLimits(raw_max_chars=2*1024*1024, result_max_chars=30_000, head_chars=20_000, tail_chars=8_000)` frozen dataclass
  - `ShellExecutionResult` 增 `full_output_path: Path | None = None`
  - `PersistentShell.__init__` 增 `output_dir: Path | None = None`、`limits: OutputLimits | None = None`
  - `ShellSessionManager.get_or_create/restart` 构造 `PersistentShell` 时传 `output_dir=workspace/.pickel/shell-output/<session_id>`

- [ ] **Step 1: 写失败测试**

```python
class OutputLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_output_truncates_and_writes_full_file(self) -> None:
        from pickel.tools.shell import OutputLimits, PersistentShell

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            out_dir = workspace / ".pickel" / "shell-output" / "s1"
            shell = PersistentShell(
                workspace_path=workspace,
                output_dir=out_dir,
                limits=OutputLimits(
                    raw_max_chars=100_000, result_max_chars=200, head_chars=120, tail_chars=50
                ),
            )
            try:
                result = shell.exec("seq 1 500")
            finally:
                shell.terminate()

        self.assertTrue(result.truncated)
        self.assertIn("truncated", result.stdout)
        self.assertIsNotNone(result.full_output_path)
        full = result.full_output_path.read_text(encoding="utf-8")
        self.assertIn("500", full)
        self.assertLess(len(result.stdout), 400)

    async def test_short_output_not_truncated(self) -> None:
        from pickel.tools.shell import PersistentShell

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo short")
            finally:
                shell.terminate()

        self.assertFalse(result.truncated)
        self.assertIsNone(result.full_output_path)
```

- [ ] **Step 2: 确认失败**（`full_output_path`/`output_dir`/`limits` 不存在 → TypeError）
- [ ] **Step 3: 实现**

```python
@dataclass(frozen=True)
class OutputLimits:
    raw_max_chars: int = 2 * 1024 * 1024   # 采集缓冲上限，超过丢中间
    result_max_chars: int = 30_000          # 注入结果上限
    head_chars: int = 20_000
    tail_chars: int = 8_000
```

`ShellExecutionResult` 增字段 `full_output_path: Path | None = None`。

`PersistentShell.__init__` 增参数并保存 `self._output_dir` / `self._limits = limits or OutputLimits()` / `self._output_seq = 0`。

raw 档（`_read_until_marker` 读循环里，chunk 追加后）：

```python
            if len(buffer) > self._limits.raw_max_chars:
                keep_head = self._limits.raw_max_chars // 2
                keep_tail = self._limits.raw_max_chars // 4
                buffer = (
                    buffer[:keep_head]
                    + f"\n... [raw output dropped] ...\n"
                    + buffer[-keep_tail:]
                )
```

结果档（marker 命中后、`_normalize_output` 之后）：

```python
    def _finalize_output(self, output: str) -> tuple[str, bool, Path | None]:
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
```

marker 命中返回处改为：

```python
                output, truncated, full_path = self._finalize_output(
                    _normalize_output(buffer[: marker_match.start()])
                )
                ...
                return ShellExecutionResult(
                    stdout=output, ..., truncated=truncated, full_output_path=full_path,
                )
```

超时/终止两个返回点同样过 `_finalize_output`。

`ShellSessionManager.get_or_create` 与 `restart` 里构造 `PersistentShell` 时传：

```python
                shell=PersistentShell(
                    workspace_path=workspace_path.resolve(),
                    process=PtyShellProcess(shell_program=self.shell_program),
                    output_dir=workspace_path.resolve() / ".pickel" / "shell-output" / session_id,
                ),
```

`ShellExecTool.execute` 的 metadata 增 `"full_output_path": str(result.full_output_path) if result.full_output_path else None`。

- [ ] **Step 4: 通过 + 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "feat(shell): 输出三档上限，超限截断并落盘引用"`

---

### Task 3: stderr 分离

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces:
  - `PtyShellProcess`：spawn 改 `stderr=subprocess.PIPE`；增 `read_chunks(timeout_ms) -> tuple[str, str]`（stdout, stderr）；`read_chunk` 删除（内部无他用）
  - `ShellExecutionResult.stderr` 语义改为「子进程真实 stderr」；新增 `status_message: str = ""` 承载原来塞在 stderr 的合成消息（"Shell command timed out." 等）
  - `ShellExecTool` content 组装：stdout + `\n--- stderr ---\n` + stderr + `\n[status] ...`（各段非空才拼）

- [ ] **Step 1: 写失败测试**

```python
class StderrSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_is_separated_from_stdout(self) -> None:
        from pickel.tools.shell import PersistentShell

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo out-line; echo err-line >&2")
            finally:
                shell.terminate()

        self.assertIn("out-line", result.stdout)
        self.assertNotIn("err-line", result.stdout)
        self.assertIn("err-line", result.stderr)

    async def test_tool_content_appends_stderr_block(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)   # 复用文件里已有的 context 构造方式
            try:
                result = await tool.execute(
                    {"command": "echo ok; echo bad >&2"}, context
                )
            finally:
                manager.close(context.session_id)

        self.assertIn("ok", result.content)
        self.assertIn("--- stderr ---", result.content)
        self.assertIn("bad", result.content)
```

（`_context` 若文件里没有现成 helper，按现有用例的 `ToolExecutionContext(..., services=ToolServices(shell_sessions=manager))` 形态内联。）

- [ ] **Step 2: 确认失败**（err-line 现在混在 stdout）
- [ ] **Step 3: 实现**

spawn：

```python
            self._process = subprocess.Popen(
                self._spawn_command(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
                ...
            )
            self._master_fd = master_fd
            self._stderr_fd = self._process.stderr.fileno()
            os.set_blocking(self._stderr_fd, False)
```

`read_chunks`：

```python
    def read_chunks(self, timeout_ms: int) -> tuple[str, str]:
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
```

`terminate` 关闭 `_stderr_fd`（置 None）；`_drain_startup_output` 改用 `read_chunks`。

`_read_until_marker`：维护 `buffer`（stdout）与 `err_buffer` 两个累计；marker 只在 stdout 找；命中后 `read_chunks(timeout_ms=50)` 再 drain 一次 stderr；三个返回点都带 `stderr=_normalize_output(err_buffer)`。合成消息改放 `status_message`：

```python
                return ShellExecutionResult(
                    stdout=..., stderr=_normalize_output(err_buffer),
                    status_message="Shell command timed out.", ...
                )
```

`ShellExecTool.execute` content 组装：

```python
        parts = [result.stdout] if result.stdout else []
        if result.stderr:
            parts.append(f"--- stderr ---\n{result.stderr}")
        if result.status_message:
            parts.append(f"[status] {result.status_message}")
        content = "\n".join(parts)
```

metadata 增 `"stderr_chars": len(result.stderr)`。

- [ ] **Step 4: 通过**；注意既有用例若断言了 `content == stdout` 形态需按新组装核对（正常路径无 stderr 时行为不变）
- [ ] **Step 5: Commit** `git commit -m "feat(shell): stderr 走独立 pipe 与 stdout 分离"`

---

### Task 4: 超时不杀会话 + pending 状态

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces:
  - `ShellStatus.RUNNING = "running"`（前台命令仍在执行）
  - `PersistentShell.pending: bool` property；`_pending_marker: str | None`
  - 超时路径：**不发任何信号**，返回 `timed_out=True, shell_status=RUNNING`，会话保活
  - `PersistentShell.exec` 在 pending 时抛 `RuntimeError("A foreground command is still running...")`
  - `PtyShellProcess.foreground_pgid() -> int | None`、`signal_foreground(sig) -> bool`（Task 5 用，本任务先落）

- [ ] **Step 1: 写失败测试**

```python
class TimeoutKeepsSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_partial_output_and_keeps_session(self) -> None:
        from pickel.tools.shell import PersistentShell, ShellStatus

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo before-sleep; sleep 30", timeout_ms=500)

                self.assertTrue(result.timed_out)
                self.assertEqual(ShellStatus.RUNNING, result.shell_status)
                self.assertIn("before-sleep", result.stdout)
                self.assertTrue(shell.is_alive())      # 会话没被杀
                self.assertTrue(shell.pending)
            finally:
                shell.terminate()

    async def test_exec_while_pending_raises(self) -> None:
        from pickel.tools.shell import PersistentShell

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                shell.exec("sleep 30", timeout_ms=300)
                with self.assertRaises(RuntimeError):
                    shell.exec("echo nope")
            finally:
                shell.terminate()
```

- [ ] **Step 2: 确认失败**（现状超时即 terminate，`is_alive()` False；无 `pending`）
- [ ] **Step 3: 实现**

`ShellStatus` 增 `RUNNING = "running"`。

`PtyShellProcess` 增（本任务落地、Task 5 使用）：

```python
    def foreground_pgid(self) -> int | None:
        if self._master_fd is None or self._process is None:
            return None
        try:
            pgid = os.tcgetpgrp(self._master_fd)
        except OSError:
            return None
        if pgid <= 0 or pgid == self._process.pid:
            return None      # 没有前台命令（前台就是 shell 自己）
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
```

`PersistentShell`：

- `__init__` 增 `self._pending_marker: str | None = None`
- `@property def pending(self) -> bool: return self._pending_marker is not None`
- `exec` 开头：`if self.pending: raise RuntimeError("A foreground command is still running; use shell_wait / shell_stdin / shell_interrupt first")`
- `exec` 写命令前 `self._pending_marker = marker`；`_read_until_marker` marker 命中与 TERMINATED 两个返回点清 `self._pending_marker = None`；**超时返回点不清、不发信号**：

```python
            if remaining_ms <= 0:
                stdout, truncated, full_path = self._finalize_output(_normalize_output(buffer))
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
```

`ShellExecTool.execute` 把 `session.shell.exec` 的 `RuntimeError` 转成 is_error 结果（提示三件套）。`is_error` 判定改为 `result.exit_code != 0 or result.shell_status not in (ShellStatus.READY, ShellStatus.RUNNING)`——超时是可续状态，交给模型决策，但 exit_code=124 仍标 error 引起注意（保持现状语义）。

- [ ] **Step 4: 通过 + 既有超时用例更新**（`test_shell_exec_allows_timeout_override` 等原本断言 TERMINATED/杀会话的，按新语义改断言 RUNNING + 会话保活）
- [ ] **Step 5: Commit** `git commit -m "feat(shell): 超时不杀会话，进入 pending 可续状态"`

---

### Task 5: 前台交互三件套

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces:
  - `PersistentShell.wait_foreground(timeout_ms) -> ShellExecutionResult`（续等 pending marker）
  - `PersistentShell.write_stdin(text, newline=True) -> ShellExecutionResult`（写入 + 300ms 窗口增量读）
  - `PersistentShell.interrupt_foreground(kill=False) -> ShellExecutionResult`（SIGINT/SIGKILL 前台组 → 等 2s marker → 失败且 shell 死才 TERMINATED）
  - 工具类 `ShellWaitTool`（`shell_wait`，参数 `timeout_ms`）、`ShellStdinTool`（`shell_stdin`，参数 `text`、`newline`）、`ShellInterruptTool`（`shell_interrupt`，参数 `kill`）

- [ ] **Step 1: 写失败测试**

```python
class ForegroundInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_picks_up_completion_after_timeout(self) -> None:
        from pickel.tools.shell import PersistentShell, ShellStatus

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("sleep 1; echo late-done", timeout_ms=200)
                self.assertTrue(first.timed_out)

                second = shell.wait_foreground(timeout_ms=3000)

                self.assertEqual(ShellStatus.READY, second.shell_status)
                self.assertIn("late-done", second.stdout)
                self.assertFalse(shell.pending)
            finally:
                shell.terminate()

    async def test_stdin_feeds_interactive_read(self) -> None:
        from pickel.tools.shell import PersistentShell, ShellStatus

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("read -r line; echo got:$line", timeout_ms=300)
                self.assertTrue(first.timed_out)

                shell.write_stdin("hello-stdin")
                final = shell.wait_foreground(timeout_ms=2000)

                self.assertEqual(ShellStatus.READY, final.shell_status)
                self.assertIn("got:hello-stdin", final.stdout)
            finally:
                shell.terminate()

    async def test_interrupt_recovers_ready_session(self) -> None:
        from pickel.tools.shell import PersistentShell, ShellStatus

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("sleep 60", timeout_ms=200)
                self.assertTrue(first.timed_out)

                result = shell.interrupt_foreground()

                self.assertEqual(ShellStatus.READY, result.shell_status)
                self.assertTrue(shell.is_alive())
                self.assertFalse(shell.pending)
                follow_up = shell.exec("echo alive-again")
                self.assertIn("alive-again", follow_up.stdout)
            finally:
                shell.terminate()

    async def test_wait_without_pending_is_error(self) -> None:
        from pickel.tools.shell import PersistentShell

        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                shell.start()
                with self.assertRaises(RuntimeError):
                    shell.wait_foreground(timeout_ms=100)
            finally:
                shell.terminate()
```

- [ ] **Step 2: 确认失败**（方法不存在）
- [ ] **Step 3: 实现**

`PersistentShell`：

```python
    def wait_foreground(self, timeout_ms: int | None = None) -> ShellExecutionResult:
        marker = self._require_pending()
        return self._read_until_marker(
            marker,
            timeout_ms=self.default_timeout_ms if timeout_ms is None else timeout_ms,
        )

    def write_stdin(self, text: str, *, newline: bool = True) -> ShellExecutionResult:
        self._require_pending()
        self.process.write(text + ("\n" if newline else ""))
        return self._read_pending_window(window_ms=300)

    def interrupt_foreground(self, *, kill: bool = False) -> ShellExecutionResult:
        marker = self._require_pending()
        sig = signal.SIGKILL if kill else signal.SIGINT
        self.process.signal_foreground(sig)
        result = self._read_until_marker(marker, timeout_ms=2000)
        if result.shell_status is ShellStatus.RUNNING and not kill:
            # SIGINT 无效，升级 SIGKILL 再试一轮
            self.process.signal_foreground(signal.SIGKILL)
            result = self._read_until_marker(marker, timeout_ms=2000)
        if result.shell_status is ShellStatus.RUNNING:
            # 前台杀不掉且 marker 不出 —— shell 已不可用，弃会话
            self.terminate()
            self._pending_marker = None
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
```

（`write_stdin`/`_read_pending_window` 超时返回即 RUNNING 增量结果，语义正好。）

三个工具类（描述文案完整写给模型）：

```python
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
                "timeout_ms": {"type": "integer", "minimum": 1,
                               "description": "How long to wait this time, in milliseconds."},
            },
        },
    )

    async def execute(self, arguments, context):
        manager = _require_shell_manager(context)
        session = manager.get(context.session_id)
        if session is None or not session.shell.pending:
            return ToolExecutionResult(
                content="No foreground command is pending.", is_error=True
            )
        timeout_ms = arguments.get("timeout_ms")
        result = await asyncio.to_thread(
            session.shell.wait_foreground,
            int(timeout_ms) if timeout_ms is not None else None,
        )
        return _foreground_result(result)
```

`ShellStdinTool`（`text` required string、`newline` boolean default True）与 `ShellInterruptTool`（`kill` boolean default False）同构；共用组装：

```python
def _foreground_result(result: ShellExecutionResult) -> ToolExecutionResult:
    parts = [result.stdout] if result.stdout else []
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    if result.status_message:
        parts.append(f"[status] {result.status_message}")
    return ToolExecutionResult(
        content="\n".join(parts) or "(no new output)",
        is_error=result.shell_status is ShellStatus.TERMINATED,
        metadata={
            "cwd": str(result.cwd),
            "exit_code": result.exit_code,
            "shell_status": result.shell_status,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        },
    )
```

`ShellExecTool` 的 content 组装改用 `_foreground_result` 同款逻辑（去重）。文件顶部补 `import asyncio`。

- [ ] **Step 4: 通过 + 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "feat(shell): shell_wait/shell_stdin/shell_interrupt 前台交互三件套"`

---

### Task 6: 工具层异步化

**Files:**
- Modify: `src/pickel/tools/shell.py`（`ShellExecTool.execute` 的阻塞调用移 to_thread）
- Test: `tests/tools/test_shell.py`

- [ ] **Step 1: 写失败测试**

```python
class EventLoopNotBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_does_not_block_event_loop(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.05)
                ticks += 1

        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            try:
                _, _ = await asyncio.gather(
                    tool.execute({"command": "sleep 0.6; echo done"}, context),
                    ticker(),
                )
            finally:
                manager.close(context.session_id)

        # exec 若阻塞 loop，ticker 只能在 exec 结束后追赶，ticks 会明显少
        self.assertGreaterEqual(ticks, 8)
```

- [ ] **Step 2: 确认失败**（现状同步 exec 卡 loop，ticks 接近 0）
- [ ] **Step 3: 实现**

`ShellExecTool.execute` 的 `session.shell.exec(...)` 改：

```python
        try:
            result = await asyncio.to_thread(
                session.shell.exec,
                str(arguments["command"]),
                int(timeout_ms) if timeout_ms is not None else None,
            )
        except RuntimeError as exc:
            return ToolExecutionResult(content=str(exc), is_error=True)
```

（`exec` 签名 `exec(self, command, timeout_ms=None)` 位置参数即可。Task 5 的三件套已用 to_thread。）

- [ ] **Step 4: 通过 + 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "fix(shell): exec 移入线程池，不再阻塞事件循环"`

---

### Task 7: 后台任务

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces:
  - `BackgroundTask` dataclass：`task_id/command/process/started_at`；线程安全缓冲 `read_output(since=0) -> tuple[str, int]`；`status() -> str`（"running"/"exited"）；`exit_code`
  - `ShellSessionManager.start_background(session_id, workspace_path, command) -> BackgroundTask`
  - `ShellSessionManager.background_tasks(session_id) -> list[BackgroundTask]`
  - `ShellSessionManager.get_background(session_id, task_id) -> BackgroundTask | None`
  - `ShellSessionManager.kill_background(session_id, task_id) -> bool`
  - `close(session_id)` 顺带终止全部后台任务
  - `shell_exec` 增 `background: bool` 参数；工具 `ShellTasksTool`/`ShellOutputTool`/`ShellKillTool`

- [ ] **Step 1: 写失败测试**

```python
class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_exec_returns_task_id_and_output_is_pollable(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            try:
                started = await tool.execute(
                    {"command": "echo bg-line; sleep 0.2; echo bg-done", "background": True},
                    context,
                )
                self.assertFalse(started.is_error)
                task_id = started.metadata["task_id"]

                output_tool = ShellOutputTool()
                deadline = time.monotonic() + 5
                text = ""
                while time.monotonic() < deadline:
                    polled = await output_tool.execute({"task_id": task_id}, context)
                    text += polled.content
                    if "bg-done" in text:
                        break
                    await asyncio.sleep(0.1)
                self.assertIn("bg-line", text)
                self.assertIn("bg-done", text)
            finally:
                manager.close(context.session_id)

    async def test_tasks_lists_and_kill_terminates(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            try:
                started = await tool.execute(
                    {"command": "sleep 60", "background": True}, context
                )
                task_id = started.metadata["task_id"]

                listed = await ShellTasksTool().execute({}, context)
                self.assertIn(task_id, listed.content)

                killed = await ShellKillTool().execute({"task_id": task_id}, context)
                self.assertFalse(killed.is_error)
                task = manager.get_background(context.session_id, task_id)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and task.status() == "running":
                    await asyncio.sleep(0.05)
                self.assertEqual("exited", task.status())
            finally:
                manager.close(context.session_id)

    async def test_close_kills_background_tasks(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            started = await tool.execute(
                {"command": "sleep 60", "background": True}, context
            )
            task = manager.get_background(context.session_id, started.metadata["task_id"])

            manager.close(context.session_id)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and task.status() == "running":
                await asyncio.sleep(0.05)
            self.assertEqual("exited", task.status())
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

```python
class BackgroundTask:
    """独立 pty 上跑的一条后台命令。reader 线程持续采集输出。"""

    def __init__(self, *, task_id: str, command: str, workspace_path: Path,
                 shell_program: str, limits: OutputLimits | None = None) -> None:
        self.task_id = task_id
        self.command = command
        self.started_at = time.time()
        self._limits = limits or OutputLimits()
        self._lock = threading.Lock()
        self._buffer = ""
        self._process = PtyShellProcess(shell_program=shell_program)
        self._spawn(workspace_path)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _spawn(self, workspace_path: Path) -> None:
        # 复用 PtyShellProcess 的 pty 装配，但命令是一次性的 bash -c
        self._process.spawn(workspace_path)
        self._process.write(self.command + "\nexit $?\n")

    def _read_loop(self) -> None:
        while self._process.is_alive():
            out, err = self._process.read_chunks(timeout_ms=200)
            if out or err:
                with self._lock:
                    self._buffer += out + err
                    if len(self._buffer) > self._limits.raw_max_chars:
                        keep = self._limits.raw_max_chars // 2
                        self._buffer = (
                            self._buffer[:keep] + "\n... [dropped] ...\n"
                            + self._buffer[-keep // 2 :]
                        )
        # 进程退出后 drain 残余
        out, err = self._process.read_chunks(timeout_ms=100)
        if out or err:
            with self._lock:
                self._buffer += out + err

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
```

（实现时若 `spawn + write + exit` 路径在测试中不稳，退化为直接 `subprocess.Popen(["bash","--noprofile","--norc","--noediting","-c", command], ...)` 挂 pty——两种都可，以测试绿为准。）

`ShellSessionManager`：

```python
    def __init__(self, shell_program: str = "/bin/bash") -> None:
        ...
        self._background: dict[str, dict[str, BackgroundTask]] = {}

    def start_background(self, session_id: str, workspace_path: Path, command: str) -> BackgroundTask:
        task = BackgroundTask(
            task_id=uuid4().hex[:8], command=command,
            workspace_path=workspace_path.resolve(), shell_program=self.shell_program,
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
```

`close(session_id)` 里对该会话全部任务 `task.kill()` 后清掉记账。

`ShellExecTool`：input_schema 增 `"background": {"type": "boolean", "description": "Run in the background; returns a task_id to poll with shell_output."}`；execute 开头：

```python
        if arguments.get("background"):
            task = manager.start_background(
                context.session_id, context.workspace_path, str(arguments["command"])
            )
            return ToolExecutionResult(
                content=f"Background task started: {task.task_id}",
                metadata={"task_id": task.task_id, "background": True},
            )
```

三个新工具：`ShellTasksTool`（无参，按行列出 `task_id  status  runtime  command[:60]`）、`ShellOutputTool`（`task_id` required、`since` integer optional，返回增量文本 + metadata `next_since`/`status`/`exit_code`）、`ShellKillTool`（`task_id` required，kill 后返回状态）。找不到 task_id 一律 is_error + 现存 id 列表。

文件顶部补 `import threading`。

- [ ] **Step 4: 通过 + 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "feat(shell): 后台任务与 shell_tasks/shell_output/shell_kill"`

---

### Task 8: 危险命令拦截

**Files:**
- Modify: `src/pickel/tools/shell.py`
- Test: `tests/tools/test_shell.py`

**Interfaces:**
- Produces: `_dangerous_command_reason(command) -> str | None`；`ShellExecTool.execute` 入口拦截（前台与 background 都过）

- [ ] **Step 1: 写失败测试**

```python
class DangerousCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _exec(self, command: str) -> ToolExecutionResult:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            try:
                return await tool.execute({"command": command}, context)
            finally:
                manager.close(context.session_id)

    async def test_blocks_rm_rf_root_and_home(self) -> None:
        for cmd in ("rm -rf /", "rm -fr /", "sudo rm -rf /*", "rm -rf ~", "rm -rf $HOME"):
            result = await self._exec(cmd)
            self.assertTrue(result.is_error, cmd)
            self.assertIn("blocked", result.content.lower(), cmd)

    async def test_blocks_mkfs_dd_forkbomb_chmod(self) -> None:
        for cmd in (
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "chmod -R 777 /",
        ):
            result = await self._exec(cmd)
            self.assertTrue(result.is_error, cmd)

    async def test_allows_normal_rm_and_workspace_paths(self) -> None:
        for cmd in ("rm -rf ./build", "rm -rf node_modules", "echo 'rm -rf /' 只是文本"):
            result = await self._exec(cmd)
            self.assertFalse(result.is_error, cmd)
```

（`echo 'rm -rf /'` 用例：规则须避开引号内文本——实现按「引号剥离后再匹配」处理不了就放宽为整条命令匹配但要求 rm 位于命令位置；以测试绿为准，误杀比漏杀更不可接受的是**误杀**。）

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

```python
# 危险命令静态拦截：挡「明显自杀」，不做 shell 解析级对抗（真防线在 S2 sandbox）
_DANGEROUS_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+(?:-\w+\s+)*-\w*[rR]\w*\s+"
            r"(?:-\w+\s+)*(/|~|\$HOME)(?:/\*)?\s*(?:$|[;&|])"
        ),
        "recursive delete of / or home",
    ),
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?mkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "raw write to block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (
        re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?ch(?:mod|own)\s+(?:-\w+\s+)*-R\s+\S+\s+/\s*(?:$|[;&|])"),
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
```

`ShellExecTool.execute` 在 background 分支之前：

```python
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
```

- [ ] **Step 4: 通过（正则按测试微调，放行用例优先）+ 全量分布不变**
- [ ] **Step 5: Commit** `git commit -m "feat(shell): 危险命令静态拦截"`

---

### Task 9: 注册、白名单、验收与文档校对

**Files:**
- Modify: `src/pickel/tools/catalog.py`（注册六个新工具）
- Modify: `agents/Pickle/agent.yaml`（白名单增补）
- Modify: `docs/upgrade/2026-07-26-shell-upgrade-design.md`（§3.2/§3.4 按语义精化改齐）
- Test: `tests/tools/test_builtin.py`、`tests/tools/test_shell.py`

- [ ] **Step 1: catalog 注册**

`builtin_tools()` 列表 shell 段追加：

```python
        ShellWaitTool(),
        ShellStdinTool(),
        ShellInterruptTool(),
        ShellTasksTool(),
        ShellOutputTool(),
        ShellKillTool(),
```

import 对应补。`tests/tools/test_builtin.py::test_builtin_tool_catalog_can_seed_bus` 与 `tests/tools/test_shell.py::test_builtin_catalog_registers_shell_tools` 的名单更新（17 个内置工具）。

- [ ] **Step 2: agent.yaml 白名单**

`agents/Pickle/agent.yaml` 的 `tools:` 增六行：`shell_wait`、`shell_stdin`、`shell_interrupt`、`shell_tasks`、`shell_output`、`shell_kill`。

- [ ] **Step 3: 全量测试**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c
```

Expected: 失败分布 = 基线 12 例（全缺 key）。

- [ ] **Step 4: 手动验收**

```bash
set -a; . ~/.pickel/.env; set +a; uv run pickel chat
```

- `seq 1 100000` → 截断 + 落盘路径可读
- `sleep 30`（带小 timeout）→ RUNNING、`shell_wait` 续等、`shell_interrupt` 恢复、后续命令正常
- `read -r x; echo $x` 超时后 `shell_stdin` 喂入
- `sleep 5 && echo done` 带 `background: true` → `shell_tasks` / `shell_output` / `shell_kill`
- `rm -rf /` 被拦截；`rm -rf ./tmp-dir` 放行
- `ls --color=always` 无转义序列

- [ ] **Step 5: 设计稿校对**

按 Global Constraints 的语义精化改 `§3.2`（阶梯移入 `shell_interrupt`）与 `§3.4`（超时即 pending、不发信号）；核对 §4 工具面与实现一致；§8 增补实施中的新取舍。

- [ ] **Step 6: Commit**

```bash
git add -A src/pickel tests/ agents/ docs/
git commit -m "feat(shell): 注册六个新 shell 工具并更新白名单与设计稿"
```

---

## 完成标准

1. 八个设计问题全部落地：ANSI 剥离、三档上限、stderr 分离、超时保会话、三件套、异步化、后台任务、危险拦截。
2. 全量失败分布 = 12 例基线（全缺 key）；`tests/tools/test_shell.py` 全绿。
3. 手动验收清单全过。
4. 设计稿与实现一致。

## 已知不在本计划内

| 项 | 归属 |
| --- | --- |
| bubblewrap / 网络 allowlist / 凭据 mask / backend 抽象 | S2 |
| 后台任务完成推 runtime 事件 | E2 之后 |
| 上限值配置化 | 需要时再做 |
| zsh 路径的 `+o zle`（对应 bash `--noediting`） | 顺手可做，无测试覆盖，留 S2 backend 抽象时一并 |
