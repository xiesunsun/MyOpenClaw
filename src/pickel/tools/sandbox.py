"""进程级沙箱策略：bubblewrap 参数生成 + 凭据环境变量剥离。

接线点只有一个——PtyShellProcess.spawn。前台 shell 与后台任务共用它，
所以一处生效即全覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import logging
from pathlib import Path

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
