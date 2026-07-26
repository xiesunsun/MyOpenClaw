"""工具运行期服务容器。

宿主提供给进程内工具的服务。extension 工具也在进程内跑，但它在装载时
用闭包持有自己的依赖，只从这里取宿主服务；服务种类由 core 决定、数量有限，
一个字段明确的 dataclass 就够，不做「能力声明 + 按需注入」。
S2 沙箱化时从这里替换实现即可，工具侧代码不动。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # 运行期不导入，避免 base ↔ shell / file_service 循环
    from pickel.tools.file_service import WorkspaceFileService
    from pickel.tools.shell import ShellSessionManager


class ActivationControl(Protocol):
    """让工具收窄/恢复激活集的窄接口。实现方是 Run。

    做成窄协议而非直接把 Run 塞进 context：工具层不该认识运行层。
    """

    def allowed_names(self) -> frozenset[str]: ...

    def disable_tools(self, names: Iterable[str]) -> None: ...

    def enable_tools(self, names: Iterable[str]) -> None: ...


@dataclass(frozen=True)
class ToolServices:
    workspace_files: "WorkspaceFileService | None" = None
    shell_sessions: "ShellSessionManager | None" = None
    activation_control: ActivationControl | None = None
