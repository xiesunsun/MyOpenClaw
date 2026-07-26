"""Extension 宿主：发现、装载并收集 extension 的贡献。

与 pickel.extensions（内置 extension 存放处）区分：本包是 core 侧宿主。
"""

from pickel.extensions_host.errors import ExtensionConfigError, ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry

__all__ = [
    "AgentScope",
    "ExtensionConfigError",
    "ExtensionHost",
    "ExtensionLoadError",
    "ExtensionRegistry",
]
