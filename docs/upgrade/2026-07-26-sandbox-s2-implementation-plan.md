# S2 进程级沙箱实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 shell 会话（前台 + 后台任务）加 bubblewrap 进程级沙箱、凭据保护与拒写自身，接线只在 `PtyShellProcess.spawn` 一处。

**Architecture:** 新模块 `src/pickel/tools/sandbox.py` 承载 `SandboxPolicy`（settings 解析、env 过滤、bwrap 参数生成、可用性探测）；`PtyShellProcess.spawn` 用它包裹 shell 命令并过滤环境；`AppConfig.sandbox` 承配置，`Boot.build_run` 构造带 policy 的 `ShellSessionManager`。

**Tech Stack:** Python 3.12、bubblewrap 0.9.0、pydantic、pytest。

## Global Constraints

- 设计稿：`docs/upgrade/2026-07-26-sandbox-s2-design.md`。范围外：网络 proxy、backend 抽象、MCP 子进程沙箱、mask 注入、seccomp/Landlock。
- **`--new-session` 必需**：缺它 bwrap 内 bash 的 job control 失效（`$-` 无 `m`、`tcgetpgrp` 返回 0），S1 的超时探测与 `shell_interrupt` 全废。
- **`--dev`（不是 `--dev-bind`）**：最小权限，实测 job control 正常，代价是沙箱内 `tty` 报 `/dev/console`。
- `PtyShellProcess.foreground_pgid()` 的 `pgid <= 0 → None` 保护不可移除：`killpg(0, sig)` 会打到调用方自己的进程组。
- 默认开；bwrap 缺失时 warning 降级裸跑（env 剥离仍生效）；`sandbox.strict: true` 时缺失即抛错。
- 环境前置（已在本机完成）：`/etc/apparmor.d/bwrap` 的 `userns,` profile + `systemctl reload apparmor`，否则 bwrap 报 `setting up uid map: Permission denied`。
- 测试命令：`uv run --with pytest --with pytest-asyncio pytest <path> -q`。全量基线：12 failed（全缺 key），分布不得变化。
- 凭证不写进代码或提交。

---

### Task 1: SandboxPolicy 模型与 env 过滤

**Files:**
- Create: `src/pickel/tools/sandbox.py`
- Test: `tests/tools/test_sandbox.py`

**Interfaces:**
- Produces:
  - `SandboxSettings(BaseModel)`：`enabled: bool = True`、`strict: bool = False`、`allow_disable: bool = False`、`allow_write: list[str] = []`、`deny_read: list[str] = []`、`env_deny: list[str] = []`、`env_allow: list[str] = []`
  - `SandboxPolicy`：`SandboxPolicy.from_settings(settings: SandboxSettings | None, *, home: Path, project_root: Path) -> SandboxPolicy`；`filter_env(env: dict[str, str]) -> dict[str, str]`；属性 `enabled`/`strict`/`allow_disable`
  - `_CREDENTIAL_ENV_PATTERNS`：模块级元组，默认剥离模式

- [ ] **Step 1: 写失败测试**（`tests/tools/test_sandbox.py`）

```python
from pathlib import Path
import unittest

from pickel.tools.sandbox import SandboxPolicy, SandboxSettings


def _policy(**kwargs) -> SandboxPolicy:
    return SandboxPolicy.from_settings(
        SandboxSettings(**kwargs),
        home=Path("/home/u/.pickel"),
        project_root=Path("/proj"),
    )


class EnvFilterTests(unittest.TestCase):
    def test_default_patterns_strip_credential_shaped_names(self) -> None:
        policy = _policy()
        filtered = policy.filter_env({
            "PATH": "/usr/bin",
            "OPENVIKING_API_KEY": "secret",
            "github_token": "secret",
            "MY_SECRET": "secret",
            "DB_PASSWORD": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path",
            "AWS_ACCESS_KEY_ID": "secret",
            "HOME": "/home/u",
        })
        self.assertEqual({"PATH": "/usr/bin", "HOME": "/home/u"}, filtered)

    def test_env_deny_adds_exact_names(self) -> None:
        policy = _policy(env_deny=["MY_PLAIN_VAR"])
        filtered = policy.filter_env({"MY_PLAIN_VAR": "x", "KEEP": "y"})
        self.assertEqual({"KEEP": "y"}, filtered)

    def test_env_allow_exempts_from_default_patterns(self) -> None:
        policy = _policy(env_allow=["GITHUB_TOKEN"])
        filtered = policy.filter_env({"GITHUB_TOKEN": "t", "OTHER_TOKEN": "x"})
        self.assertEqual({"GITHUB_TOKEN": "t"}, filtered)

    def test_disabled_policy_keeps_everything(self) -> None:
        policy = _policy(enabled=False)
        env = {"OPENVIKING_API_KEY": "secret", "PATH": "/usr/bin"}
        self.assertEqual(env, policy.filter_env(env))
```

- [ ] **Step 2: 确认失败** `uv run --with pytest --with pytest-asyncio pytest tests/tools/test_sandbox.py -q` → ImportError
- [ ] **Step 3: 实现**（`src/pickel/tools/sandbox.py`）

```python
"""进程级沙箱策略：bubblewrap 参数生成 + 凭据环境变量剥离。

接线点只有一个——PtyShellProcess.spawn。前台 shell 与后台任务共用它，
所以一处生效即全覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import logging
from pathlib import Path
import shutil

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 凭据形状的环境变量名（大小写不敏感）；命中即从 shell 环境剥离
_CREDENTIAL_ENV_PATTERNS = (
    "*_API_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_PASSWORD",
    "*_CREDENTIALS",
    "*_ACCESS_KEY",
    "*_ACCESS_KEY_ID",
    "*_SECRET_ACCESS_KEY",
)

# 默认读拒绝目录（相对 home），存在才挂 tmpfs
_DEFAULT_DENY_READ_HOME_DIRS = (
    ".ssh",
    ".aws",
    ".config/gcloud",
    ".kube",
    ".docker",
)


class SandboxSettings(BaseModel):
    enabled: bool = True
    strict: bool = False
    allow_disable: bool = False
    allow_write: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    env_deny: list[str] = Field(default_factory=list)
    env_allow: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool = True
    strict: bool = False
    allow_disable: bool = False
    pickel_home: Path = Path.home() / ".pickel"
    project_root: Path = Path.cwd()
    allow_write: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()
    env_deny: frozenset[str] = frozenset()
    env_allow: frozenset[str] = frozenset()

    @classmethod
    def from_settings(
        cls,
        settings: SandboxSettings | None,
        *,
        home: Path,
        project_root: Path,
    ) -> SandboxPolicy:
        resolved = settings or SandboxSettings()
        return cls(
            enabled=resolved.enabled,
            strict=resolved.strict,
            allow_disable=resolved.allow_disable,
            pickel_home=Path(home),
            project_root=Path(project_root),
            allow_write=tuple(Path(item).expanduser() for item in resolved.allow_write),
            deny_read=tuple(Path(item).expanduser() for item in resolved.deny_read),
            env_deny=frozenset(name.upper() for name in resolved.env_deny),
            env_allow=frozenset(name.upper() for name in resolved.env_allow),
        )

    def filter_env(self, env: dict[str, str]) -> dict[str, str]:
        """剥离凭据形状的环境变量。与 bwrap 无关——降级裸跑时也生效。"""
        if not self.enabled:
            return dict(env)
        return {
            name: value
            for name, value in env.items()
            if not self._is_credential(name)
        }

    def _is_credential(self, name: str) -> bool:
        upper = name.upper()
        if upper in self.env_allow:
            return False
        if upper in self.env_deny:
            return True
        return any(
            fnmatch.fnmatchcase(upper, pattern)
            for pattern in _CREDENTIAL_ENV_PATTERNS
        )
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/tools/test_sandbox.py -q`
- [ ] **Step 5: Commit** `git add src/pickel/tools/sandbox.py tests/tools/test_sandbox.py && git commit -m "feat(sandbox): SandboxPolicy 模型与凭据环境变量剥离"`

---

### Task 2: bwrap 参数生成与可用性探测

**Files:**
- Modify: `src/pickel/tools/sandbox.py`
- Test: `tests/tools/test_sandbox.py`

**Interfaces:**
- Consumes: `SandboxPolicy`（Task 1）。
- Produces:
  - `SandboxUnavailableError(RuntimeError)`
  - `SandboxPolicy.wrap_command(command: list[str], *, workspace: Path) -> tuple[list[str], bool]`——返回 `(最终命令, 是否沙箱化)`；enabled=False 或 bwrap 缺失（非 strict）时原样返回 + `False`；bwrap 缺失且 strict 时抛 `SandboxUnavailableError`
  - `SandboxPolicy.self_protect_paths(workspace: Path) -> tuple[Path, ...]`——拒写自身清单（存在的才返回）

- [ ] **Step 1: 写失败测试**（追加到 `tests/tools/test_sandbox.py`）

```python
from tempfile import TemporaryDirectory
from unittest import mock

from pickel.tools.sandbox import SandboxUnavailableError


def _wrap(policy: SandboxPolicy, workspace: Path) -> tuple[list[str], bool]:
    return policy.wrap_command(["/bin/bash", "-s"], workspace=workspace)


class WrapCommandTests(unittest.TestCase):
    def _policy_for(self, tmp: Path, **kwargs) -> SandboxPolicy:
        return SandboxPolicy.from_settings(
            SandboxSettings(**kwargs), home=tmp / "home", project_root=tmp / "proj"
        )

    def test_wrapped_command_has_required_flags_and_binds(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, sandboxed = _wrap(self._policy_for(tmp), workspace)
            self.assertTrue(sandboxed)
            self.assertEqual("bwrap", argv[0])
            # --new-session 必需：缺它 bwrap 内 job control 失效
            self.assertIn("--new-session", argv)
            self.assertIn("--die-with-parent", argv)
            self.assertIn("--dev", argv)
            self.assertNotIn("--dev-bind", argv)
            joined = " ".join(argv)
            self.assertIn(f"--ro-bind / /", joined)
            self.assertIn(f"--bind {workspace} {workspace}", joined)
            self.assertIn("--bind /tmp /tmp", joined)
            self.assertEqual(["/bin/bash", "-s"], argv[argv.index("--") + 1 :])

    def test_workspace_bind_precedes_self_protect_ro_bind(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            (workspace / "src" / "pickel").mkdir(parents=True)
            (workspace / "agents").mkdir()
            policy = SandboxPolicy.from_settings(
                SandboxSettings(), home=tmp / "home", project_root=workspace
            )
            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = _wrap(policy, workspace)
            joined = " ".join(argv)
            bind_at = joined.index(f"--bind {workspace} {workspace}")
            ro_at = joined.index(f"--ro-bind {workspace / 'agents'}")
            self.assertLess(bind_at, ro_at, "self-protect 必须在 workspace bind 之后盖回")

    def test_deny_read_paths_become_tmpfs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            secret = tmp / "secret-dir"
            secret.mkdir()
            policy = self._policy_for(tmp, deny_read=[str(secret)])
            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = _wrap(policy, workspace)
            self.assertIn(f"--tmpfs {secret}", " ".join(argv))

    def test_missing_paths_are_skipped(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            policy = self._policy_for(tmp, deny_read=[str(tmp / "nope")])
            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = _wrap(policy, workspace)
            self.assertNotIn("nope", " ".join(argv))

    def test_allow_write_paths_are_bound_writable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            extra = tmp / "cache"
            extra.mkdir()
            policy = self._policy_for(tmp, allow_write=[str(extra)])
            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = _wrap(policy, workspace)
            self.assertIn(f"--bind {extra} {extra}", " ".join(argv))

    def test_disabled_policy_returns_command_unchanged(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp, enabled=False)
            argv, sandboxed = _wrap(policy, tmp)
            self.assertEqual(["/bin/bash", "-s"], argv)
            self.assertFalse(sandboxed)

    def test_missing_bwrap_degrades_when_not_strict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp)
            with mock.patch("shutil.which", return_value=None):
                argv, sandboxed = _wrap(policy, tmp)
            self.assertEqual(["/bin/bash", "-s"], argv)
            self.assertFalse(sandboxed)

    def test_missing_bwrap_raises_when_strict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp, strict=True)
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(SandboxUnavailableError):
                    _wrap(policy, tmp)
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（追加到 `src/pickel/tools/sandbox.py`）

```python
class SandboxUnavailableError(RuntimeError):
    pass


_BWRAP = "bwrap"
```

`SandboxPolicy` 增两个方法：

```python
    def self_protect_paths(self, workspace: Path) -> tuple[Path, ...]:
        """拒写自身：配置目录、agent 定义、pickel 代码。写掩盖、读放行。"""
        import pickel

        package_root = Path(pickel.__file__).resolve().parent
        candidates = [
            self.pickel_home,
            self.project_root / ".pickel",
            self.project_root / "agents",
            package_root,
        ]
        seen: list[Path] = []
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved.exists() and resolved not in seen:
                seen.append(resolved)
        return tuple(seen)

    def wrap_command(
        self, command: list[str], *, workspace: Path
    ) -> tuple[list[str], bool]:
        """把命令包进 bwrap。返回 (最终命令, 是否沙箱化)。"""
        if not self.enabled:
            return list(command), False
        if shutil.which(_BWRAP) is None:
            if self.strict:
                raise SandboxUnavailableError(
                    "bubblewrap (bwrap) is not installed and sandbox.strict is on"
                )
            logger.warning(
                "bubblewrap (bwrap) not found; running shell without sandbox. "
                "Credential env vars are still stripped."
            )
            return list(command), False

        workspace = workspace.resolve()
        argv = [
            _BWRAP,
            "--die-with-parent",
            # --new-session 必需：缺它 bwrap 内 bash 的 job control 失效，
            # 超时探测与 shell_interrupt 依赖的前台进程组就不存在了
            "--new-session",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--bind", "/tmp", "/tmp",
            "--bind", str(workspace), str(workspace),
        ]
        for path in self.allow_write:
            resolved = path.resolve()
            if resolved.exists():
                argv += ["--bind", str(resolved), str(resolved)]
        # 顺序要紧：self-protect 在 workspace bind 之后，才能把它盖回只读
        for path in self.self_protect_paths(workspace):
            argv += ["--ro-bind", str(path), str(path)]
        for path in self._deny_read_paths():
            argv += ["--tmpfs", str(path)]
        argv.append("--")
        argv.extend(command)
        return argv, True

    def _deny_read_paths(self) -> tuple[Path, ...]:
        home = self.pickel_home.expanduser().parent
        candidates = [self.pickel_home]
        candidates += [home / name for name in _DEFAULT_DENY_READ_HOME_DIRS]
        candidates += list(self.deny_read)
        seen: list[Path] = []
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved.exists() and resolved not in seen:
                seen.append(resolved)
        return tuple(seen)
```

注意 `pickel_home` 语义：它是 `~/.pickel` 本身，`_deny_read_paths` 里的 `home` 取它的父目录来拼 `~/.ssh` 等。

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/tools/test_sandbox.py -q`
- [ ] **Step 5: Commit** `git add src/pickel/tools/sandbox.py tests/tools/test_sandbox.py && git commit -m "feat(sandbox): bwrap 参数生成、拒写自身与降级/strict"`

---

### Task 3: PtyShellProcess.spawn 接线与沙箱内集成测试

**Files:**
- Modify: `src/pickel/tools/shell.py`（`PtyShellProcess.__init__` / `spawn`、`PersistentShell.__init__`、`BackgroundTask.__init__`、`ShellSessionManager.__init__` 与三处构造）
- Test: `tests/tools/test_sandbox_shell.py`

**Interfaces:**
- Consumes: `SandboxPolicy.wrap_command` / `filter_env`、`SandboxUnavailableError`（Task 2）。
- Produces:
  - `PtyShellProcess(shell_program=..., sandbox: SandboxPolicy | None = None)`；属性 `sandboxed: bool`（spawn 后可读，未 spawn 为 `False`）
  - `PersistentShell(..., sandbox: SandboxPolicy | None = None)` 透传给自建的 process
  - `BackgroundTask(..., sandbox: SandboxPolicy | None = None)`
  - `ShellSessionManager(shell_program=..., sandbox: SandboxPolicy | None = None)`：`get_or_create` / `restart` / `start_background` 都透传

- [ ] **Step 1: 写失败测试**（`tests/tools/test_sandbox_shell.py`）

```python
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from pickel.tools.sandbox import SandboxPolicy, SandboxSettings
from pickel.tools.shell import PersistentShell, ShellStatus

HAS_BWRAP = shutil.which("bwrap") is not None


def _policy(tmp: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy.from_settings(
        SandboxSettings(**kwargs), home=Path.home() / ".pickel", project_root=tmp
    )


class SandboxSpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_is_filtered_even_without_bwrap(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = PersistentShell(workspace_path=tmp, sandbox=_policy(tmp))
            try:
                with mock.patch.dict(
                    "os.environ", {"PROBE_API_KEY": "leak", "PROBE_PLAIN": "fine"}
                ):
                    with mock.patch("shutil.which", return_value=None):
                        shell.start()
                result = shell.exec("echo key=[${PROBE_API_KEY:-empty}] plain=$PROBE_PLAIN")
                self.assertIn("key=[empty]", result.stdout)
                self.assertIn("plain=fine", result.stdout)
                self.assertFalse(shell.process.sandboxed)
            finally:
                shell.terminate()


@unittest.skipUnless(HAS_BWRAP, "bubblewrap not installed")
class SandboxedShellIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _shell(self, tmp: Path) -> PersistentShell:
        return PersistentShell(workspace_path=tmp, sandbox=_policy(tmp))

    async def test_workspace_is_writable_and_system_is_not(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                self.assertTrue(shell.process.sandboxed)
                ok = shell.exec("touch ./probe && echo write-ok")
                self.assertIn("write-ok", ok.stdout)
                denied = shell.exec("touch /usr/probe 2>/dev/null && echo BAD || echo denied")
                self.assertIn("denied", denied.stdout)
            finally:
                shell.terminate()

    async def test_pickel_home_is_masked(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                result = shell.exec("ls ~/.pickel 2>/dev/null | wc -l")
                self.assertEqual("0", result.stdout.strip())
            finally:
                shell.terminate()

    async def test_job_control_survives_sandbox(self) -> None:
        """--new-session 的回归闸：缺它这条必挂。"""
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                flags = shell.exec("echo flags:$-")
                self.assertIn("m", flags.stdout.split("flags:")[1])

                timed_out = shell.exec("sleep 30", timeout_ms=300)
                self.assertIs(ShellStatus.RUNNING, timed_out.shell_status)
                interrupted = shell.interrupt_foreground()
                self.assertIs(ShellStatus.READY, interrupted.shell_status)
                self.assertTrue(shell.is_alive())
                after = shell.exec("echo alive")
                self.assertIn("alive", after.stdout)
            finally:
                shell.terminate()

    async def test_stderr_separation_survives_sandbox(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                result = shell.exec("echo out; echo err >&2")
                self.assertIn("out", result.stdout)
                self.assertIn("err", result.stderr)
                self.assertNotIn("err", result.stdout)
            finally:
                shell.terminate()
```

- [ ] **Step 2: 确认失败**（`sandbox=` 参数不存在 → TypeError）
- [ ] **Step 3: 实现**（`src/pickel/tools/shell.py`）

顶部 import 补 `from pickel.tools.sandbox import SandboxPolicy`。

`PtyShellProcess.__init__` 签名与字段：

```python
    def __init__(
        self,
        shell_program: str = "/bin/bash",
        sandbox: SandboxPolicy | None = None,
    ) -> None:
        self.shell_program = shell_program
        self.sandbox = sandbox
        self.sandboxed = False
        ...
```

`spawn` 里两处改动——环境过滤与命令包裹。找到构造 `shell_env` 的段落，在 `PROMPT_COMMAND` 赋值之后、`subprocess.Popen` 之前插入：

```python
            if self.sandbox is not None:
                shell_env = self.sandbox.filter_env(shell_env)
```

`Popen` 的第一个参数由 `self._spawn_command()` 改为包裹后的命令：

```python
            spawn_argv = self._spawn_command()
            if self.sandbox is not None:
                spawn_argv, self.sandboxed = self.sandbox.wrap_command(
                    spawn_argv, workspace=workspace_path
                )
            self._process = subprocess.Popen(
                spawn_argv,
                ...
            )
```

（`SandboxUnavailableError` 是 `RuntimeError` 子类，直接向上抛；`PersistentShell.exec` 的调用方 `ShellExecTool` 已把 `RuntimeError` 转 is_error。）

`PersistentShell.__init__` 增关键字参数 `sandbox: SandboxPolicy | None = None`，构造默认 process 时透传：

```python
        self.process = process or PtyShellProcess(sandbox=sandbox)
```

`BackgroundTask.__init__` 增 `sandbox: SandboxPolicy | None = None`，`self._process = PtyShellProcess(shell_program=shell_program, sandbox=sandbox)`。

`ShellSessionManager.__init__` 增 `sandbox: SandboxPolicy | None = None` 存 `self.sandbox`；`get_or_create` 与 `restart` 里构造 `PtyShellProcess(shell_program=self.shell_program, sandbox=self.sandbox)`；`start_background` 里 `BackgroundTask(..., sandbox=self.sandbox)`。

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/tools/ -q`（含 S1 全部 shell 用例回归）
- [ ] **Step 5: Commit** `git add src/pickel/tools tests/tools && git commit -m "feat(sandbox): spawn 接线——bwrap 包裹与 env 剥离覆盖前台与后台"`

---

### Task 4: 配置接线（AppConfig.sandbox → Boot → manager）

**Files:**
- Modify: `src/pickel/config/app_config.py`（`AppConfig` 增 `sandbox` 字段）
- Modify: `src/pickel/app/boot.py`（构造 policy 与带 policy 的 manager）
- Test: `tests/app/test_sandbox_wiring.py`

**Interfaces:**
- Consumes: `SandboxSettings` / `SandboxPolicy`（Task 1-2）、`ShellSessionManager(sandbox=...)`（Task 3）。
- Produces:
  - `AppConfig.sandbox: SandboxSettings`（默认全默认值）
  - `Boot.sandbox_policy` 属性（懒构造，`home=~/.pickel` 取 `pickel.config.paths.home_dir()`、`project_root=app_config.root`）
  - `Boot.build_run` 传 `shell_session_manager=ShellSessionManager(sandbox=self.sandbox_policy)`

- [ ] **Step 1: 写失败测试**（`tests/app/test_sandbox_wiring.py`）

```python
from pathlib import Path
import unittest

from pickel.config.app_config import AppConfig
from pickel.tools.sandbox import SandboxSettings


class SandboxConfigTests(unittest.TestCase):
    def test_app_config_defaults_sandbox_enabled(self) -> None:
        self.assertTrue(SandboxSettings().enabled)
        self.assertFalse(SandboxSettings().strict)
        self.assertFalse(SandboxSettings().allow_disable)

    def test_app_config_accepts_sandbox_section(self) -> None:
        fields = AppConfig.model_fields
        self.assertIn("sandbox", fields)
```

以及 boot 接线（fixture 与 `tests/app/test_assembly.py` 同款：yaml 文本 → `app_config_from_yaml_file` → `Boot`）：

```python
import textwrap
from tempfile import TemporaryDirectory

from pickel.app.boot import Boot
from tests.helpers.yaml_app_config import app_config_from_yaml_file

_CONFIG_YAML = """
default_agent: Pickle
default_file_access_mode: full
default_llm:
  provider: google/gemini
  model: gemini-3-flash-preview
providers:
  google/gemini:
    models:
      gemini-3-flash-preview:
        temperature: 1.0
        max_output_tokens: 1024
        provider_options: {}
sandbox:
  strict: true
agents:
  Pickle:
    workspace_path: workspace
    behavior_path: agents/Pickle
    tools:
      - shell_exec
"""


class SandboxWiringTests(unittest.TestCase):
    def test_build_run_passes_policy_to_shell_manager(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text(
                "You are Pickle.\n", encoding="utf-8"
            )
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent(_CONFIG_YAML).strip(), encoding="utf-8")

            boot = Boot(app_config_from_yaml_file(config_path))
            _, run = boot.build_run()

            policy = run.shell_session_manager.sandbox
            self.assertIsNotNone(policy)
            self.assertTrue(policy.strict)
            self.assertEqual(root.resolve(), policy.project_root.resolve())
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

`app_config.py`：import `from pickel.tools.sandbox import SandboxSettings`，`AppConfig` 增字段：

```python
    # 进程级沙箱（S2）：默认开，缺 bwrap 降级；strict 时缺依赖即拒绝
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
```

`boot.py`：import `SandboxPolicy`、`ShellSessionManager`、`home_dir`；`Boot` 增属性：

```python
    @property
    def sandbox_policy(self) -> SandboxPolicy:
        if self._sandbox_policy is None:
            self._sandbox_policy = SandboxPolicy.from_settings(
                self.app_config.sandbox,
                home=home_dir(),
                project_root=self.app_config.root,
            )
        return self._sandbox_policy
```

（`_sandbox_policy` 在 `__init__` 或 dataclass 字段里初始化为 `None`；照该文件现有风格实现。）`build_run` 的 `Run.open(...)` 增参数：

```python
            shell_session_manager=ShellSessionManager(sandbox=self.sandbox_policy),
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/app/ tests/config/ -q`（`tests/app/test_assembly.py` 的 3 例基线失败不变）
- [ ] **Step 5: Commit** `git add src/pickel/config src/pickel/app tests/app && git commit -m "feat(sandbox): 配置接线 AppConfig.sandbox → Boot → ShellSessionManager"`

---

### Task 5: 逃生口与 sandboxed metadata

**Files:**
- Modify: `src/pickel/tools/shell.py`（`ShellRestartTool`、`ShellExecTool` 的 metadata、`ShellSessionManager.restart`）
- Test: `tests/tools/test_sandbox_shell.py`

**Interfaces:**
- Consumes: `SandboxPolicy.allow_disable`（Task 1）。
- Produces:
  - `ShellSessionManager.restart(session_id, workspace_path, *, sandbox: bool = True) -> ShellSession`——`sandbox=False` 且 `policy.allow_disable` 为真时用 `enabled=False` 的策略重建
  - `ShellRestartTool` input_schema 增 `sandbox: boolean`（默认 true）
  - `ShellExecTool` / `ShellRestartTool` 的 metadata 增 `sandboxed: bool`

- [ ] **Step 1: 写失败测试**（追加到 `tests/tools/test_sandbox_shell.py`）

```python
from pickel.tools.base import ToolExecutionContext
from pickel.tools.services import ToolServices
from pickel.tools.shell import ShellExecTool, ShellRestartTool, ShellSessionManager


def _context(workspace: Path, manager: ShellSessionManager) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle", session_id="s", workspace_path=workspace,
        services=ToolServices(shell_sessions=manager),
    )


class EscapeHatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_request_is_ignored_by_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manager = ShellSessionManager(sandbox=_policy(tmp))
            context = _context(tmp, manager)
            try:
                result = await ShellRestartTool().execute({"sandbox": False}, context)
                self.assertIn("ignored", result.content.lower())
                self.assertEqual(HAS_BWRAP, result.metadata["sandboxed"])
            finally:
                manager.close(context.session_id)

    async def test_disable_is_honoured_when_allowed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manager = ShellSessionManager(sandbox=_policy(tmp, allow_disable=True))
            context = _context(tmp, manager)
            try:
                result = await ShellRestartTool().execute({"sandbox": False}, context)
                self.assertFalse(result.metadata["sandboxed"])
            finally:
                manager.close(context.session_id)

    async def test_exec_metadata_reports_sandbox_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manager = ShellSessionManager(sandbox=_policy(tmp))
            context = _context(tmp, manager)
            try:
                result = await ShellExecTool().execute({"command": "echo hi"}, context)
                self.assertEqual(HAS_BWRAP, result.metadata["sandboxed"])
            finally:
                manager.close(context.session_id)
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

`ShellSessionManager.restart` 签名与实现：

```python
    def restart(
        self, session_id: str, workspace_path: Path, *, sandbox: bool = True
    ) -> ShellSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            existing.shell.terminate()
        policy = self.sandbox
        if not sandbox and policy is not None and policy.allow_disable:
            policy = replace(policy, enabled=False)
        # ...原逻辑，构造 PtyShellProcess(shell_program=self.shell_program, sandbox=policy)
```

（`replace` 来自 `dataclasses`，文件已 import `dataclass`；补 `replace`。）

`ShellRestartTool.spec.input_schema` 改：

```python
        input_schema={
            "type": "object",
            "properties": {
                "sandbox": {
                    "type": "boolean",
                    "description": (
                        "Restart with the sandbox enabled (default true). "
                        "Requests to disable it are ignored unless the user has "
                        "set sandbox.allow_disable."
                    ),
                },
            },
        },
```

`ShellRestartTool.execute`：

```python
        manager = _require_shell_manager(context)
        requested_sandbox = bool(arguments.get("sandbox", True))
        session = manager.restart(
            context.session_id, context.workspace_path, sandbox=requested_sandbox
        )
        policy = manager.sandbox
        ignored = (
            not requested_sandbox
            and policy is not None
            and policy.enabled
            and not policy.allow_disable
        )
        message = f"Shell restarted at {session.workspace_path}"
        if ignored:
            message += (
                " (sandbox=false ignored: set sandbox.allow_disable in settings "
                "to permit unsandboxed shells)"
            )
        return ToolExecutionResult(
            content=message,
            metadata={
                "cwd": str(session.workspace_path),
                "shell_status": ShellStatus.READY,
                "restarted": True,
                "sandboxed": session.shell.process.sandboxed,
            },
        )
```

注意 `restart` 里 `session.shell.start()` 已被调用（现状如此），所以 `process.sandboxed` 此时已是实际值。

`_result_metadata` 里加 `sandboxed`——它不属于 `ShellExecutionResult`，改由 `ShellExecTool.execute` 在 metadata 增补：

```python
            metadata=_result_metadata(
                result,
                created_new_shell=created_new_shell,
                sandboxed=session.shell.process.sandboxed,
            ),
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/tools/ -q`
- [ ] **Step 5: Commit** `git add src/pickel/tools tests/tools && git commit -m "feat(sandbox): 会话级逃生口与 sandboxed metadata"`

---

### Task 6: 全量、真机验收与设计稿校对

**Files:**
- Modify: `docs/upgrade/2026-07-26-sandbox-s2-design.md`（按实施补差异）

- [ ] **Step 1: 全量测试**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c
```

Expected: 失败分布 = 基线 12 例（tests/app/test_assembly.py 3、tests/cli/test_chat_loop.py 1、tests/providers/test_gemini.py 7、tests/providers/test_model_context_generate.py 1）。

- [ ] **Step 2: 真机验收**

```bash
set -a; . ~/.pickel/.env; set +a; uv run pickel chat
```

逐条问模型执行并核对：

| 验收项 | 期望 |
| --- | --- |
| `cat ~/.pickel/.env` | 文件不存在/不可读 |
| `env \| grep -ci api_key` | 0 |
| `touch src/pickel/probe.txt` | 失败（只读） |
| `touch ./probe.txt && rm ./probe.txt` | 成功 |
| `git status` / `uv --version` | 正常 |
| `sleep 30`（小 timeout）→ `shell_interrupt` | RUNNING → READY，shell 存活 |
| `echo a; echo b >&2` | stderr 单独成段 |
| `shell_restart` 带 `sandbox: false` | content 注明被忽略，metadata `sandboxed: true` |

- [ ] **Step 3: 设计稿校对**

核对 §3 各节与实现一致（尤其 §3.4 env 模式清单、§3.6 逃生口语义），不一致处按实现改；§6 增补实施中的新取舍。

- [ ] **Step 4: Commit**

```bash
git add docs/upgrade/2026-07-26-sandbox-s2-design.md
git commit -m "docs(sandbox): 设计稿按实施校对"
```
