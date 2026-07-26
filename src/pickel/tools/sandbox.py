"""进程级沙箱策略：bubblewrap 参数生成 + 凭据环境变量剥离。

接线点只有一个——PtyShellProcess.spawn。前台 shell 与后台任务共用它，
所以一处生效即全覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import logging
from pathlib import Path
import shutil

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_BWRAP = "bwrap"


class SandboxUnavailableError(RuntimeError):
    pass

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
            name: value for name, value in env.items() if not self._is_credential(name)
        }

    def _is_credential(self, name: str) -> bool:
        upper = name.upper()
        if upper in self.env_allow:
            return False
        if upper in self.env_deny:
            return True
        return any(
            fnmatch.fnmatchcase(upper, pattern) for pattern in _CREDENTIAL_ENV_PATTERNS
        )

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
