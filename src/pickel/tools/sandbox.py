"""进程级沙箱策略：Linux Bubblewrap、macOS Seatbelt 与环境过滤。

接线点只有一个——BashSession 启动 PTY 进程时。前台 shell 与后台任务共用它，
所以一处生效即全覆盖。
"""

from __future__ import annotations

import fnmatch
import logging
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pickel.config.sandbox_settings import SandboxSettings

logger = logging.getLogger(__name__)

_BWRAP = "bwrap"
_SEATBELT = "/usr/bin/sandbox-exec"


class SandboxUnavailableError(RuntimeError):
    pass


# 凭据形状的环境变量名（大小写不敏感）；命中即从 shell 环境剥离。
# 中缀匹配而非后缀：实测 ANTHROPIC_API_KEY_PICKLE 这类命名会从 *_API_KEY 漏出去。
# 代价是误杀无害变量（如 TOKENIZERS_PARALLELISM）——用 sandbox.env_allow 豁免，
# 宁可误杀也不漏。
_CREDENTIAL_ENV_PATTERNS = (
    "*API_KEY*",
    "*APIKEY*",
    "*TOKEN*",
    "*SECRET*",
    "*PASSWORD*",
    "*PASSWD*",
    "*CREDENTIAL*",
    "*ACCESS_KEY*",
    "*PRIVATE_KEY*",
    # 兜底：带下划线前缀的 KEY 一律视为凭据（OPENVIKING_USER_KEY、SSH_KEY_PATH…）；
    # 无下划线的 MONKEY_MODE 之类不受影响
    "*_KEY*",
)

# 默认读拒绝目录（相对 home），存在才挂 tmpfs
_DEFAULT_DENY_READ_HOME_DIRS = (
    ".ssh",
    ".aws",
    ".config/gcloud",
    ".kube",
    ".docker",
)


@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool = True
    strict: bool = False
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
        """按宿主平台包裹命令，返回 ``(最终命令, 是否沙箱化)``。"""
        if not self.enabled:
            return list(command), False

        system = platform.system()
        if system == "Linux":
            return self._wrap_bubblewrap(command, workspace=workspace)
        if system == "Darwin":
            return self._wrap_seatbelt(command, workspace=workspace)
        return self._sandbox_unavailable(
            command,
            f"sandbox is not supported on {system or 'this platform'}",
        )

    def _wrap_bubblewrap(
        self, command: list[str], *, workspace: Path
    ) -> tuple[list[str], bool]:
        if shutil.which(_BWRAP) is None:
            return self._sandbox_unavailable(
                command,
                "bubblewrap (bwrap) is not installed",
            )

        workspace = workspace.resolve()
        argv = [
            _BWRAP,
            "--die-with-parent",
            # --new-session 必需：缺它 bwrap 内 bash 的 job control 失效，
            # 超时探测与 shell_interrupt 依赖的前台进程组就不存在了
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            "/tmp",
            "/tmp",
            "--bind",
            str(workspace),
            str(workspace),
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

    def _wrap_seatbelt(
        self, command: list[str], *, workspace: Path
    ) -> tuple[list[str], bool]:
        if not _seatbelt_available():
            return self._sandbox_unavailable(
                command,
                f"Seatbelt executable {_SEATBELT} is not available",
            )

        profile, definitions = self._seatbelt_profile(workspace)
        argv = [_SEATBELT, "-p", profile]
        argv.extend(f"-D{name}={path}" for name, path in definitions)
        argv.append("--")
        argv.extend(command)
        return argv, True

    def _seatbelt_profile(
        self, workspace: Path
    ) -> tuple[str, tuple[tuple[str, Path], ...]]:
        """生成以文件系统隔离为核心、兼容开发工具的 Seatbelt profile。"""
        writable = _existing_paths(
            (
                workspace,
                Path("/tmp"),
                Path(tempfile.gettempdir()),
                *self.allow_write,
            )
        )
        protected = self.self_protect_paths(workspace)
        unreadable = self._deny_read_paths()

        definitions: list[tuple[str, Path]] = []
        write_rules: list[str] = []
        for index, path in enumerate(writable):
            name = f"WRITE_{index}"
            definitions.append((name, path))
            write_rules.append(
                f'(allow file-write* (literal (param "{name}")) '
                f'(subpath (param "{name}")))'
            )

        protect_rules: list[str] = []
        for index, path in enumerate(protected):
            name = f"PROTECT_{index}"
            definitions.append((name, path))
            protect_rules.append(
                f'(deny file-write* (literal (param "{name}")) '
                f'(subpath (param "{name}")))'
            )

        unreadable_rules: list[str] = []
        for index, path in enumerate(unreadable):
            name = f"DENY_READ_{index}"
            definitions.append((name, path))
            unreadable_rules.append(
                f'(deny file-read* (literal (param "{name}")) '
                f'(subpath (param "{name}")))'
            )

        sections = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            "(allow file-read*)",
            *write_rules,
            *protect_rules,
            *unreadable_rules,
            '(allow file-write-data (literal "/dev/null"))',
            "(allow sysctl-read)",
            "(allow ipc-posix-sem)",
            "(allow ipc-posix-shm*)",
            "(allow pseudo-tty)",
            '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))',
            '(allow file-read* file-write* file-ioctl (regex #"^/dev/ttys[0-9]+"))',
            "(allow user-preference-read)",
            '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
            '(allow mach-lookup (global-name "com.apple.PowerManagement.control"))',
            '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") '
            '(global-name "com.apple.cfprefsd.agent") '
            '(local-name "com.apple.cfprefsd.agent"))',
            "(allow network*)",
            "(allow system-socket)",
            '(allow mach-lookup (global-name "com.apple.bsd.dirhelper") '
            '(global-name "com.apple.system.opendirectoryd.membership") '
            '(global-name "com.apple.SecurityServer") '
            '(global-name "com.apple.networkd") '
            '(global-name "com.apple.ocspd") '
            '(global-name "com.apple.trustd.agent") '
            '(global-name "com.apple.SystemConfiguration.DNSConfiguration") '
            '(global-name "com.apple.SystemConfiguration.configd"))',
        ]
        return "\n".join(sections), tuple(definitions)

    def _sandbox_unavailable(
        self,
        command: list[str],
        reason: str,
    ) -> tuple[list[str], bool]:
        if self.strict:
            raise SandboxUnavailableError(f"{reason} and sandbox.strict is on")
        logger.warning(
            "%s; running shell without an OS sandbox. Credential environment "
            "variables are still stripped.",
            reason,
        )
        return list(command), False

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


def _seatbelt_available() -> bool:
    # 固定系统路径，避免 PATH 中的同名程序替换安全边界。
    return Path(_SEATBELT).is_file()


def _existing_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved.exists() and resolved not in seen:
            seen.append(resolved)
    return tuple(seen)
