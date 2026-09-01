"""Extension 宿主：发现、装载并收集 extension 的贡献。

与 pickel.extensions（内置 extension 存放处）区分：本包是 core 侧宿主。
"""

from pickel.extensions_host.errors import ExtensionConfigError, ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.event_processor import (
    ConversationExtensionContext,
    EventProcessor,
    EventProcessorRegistration,
    ResolvedEventProcessor,
)
from pickel.shared.conversation_mode import ConversationMode
from pickel.extensions_host.loader import (
    LoadResult,
    extension_version_for_module,
    load_extensions,
    load_extensions_async,
    teardown_extensions,
)
from pickel.extensions_host.mcp_status import (
    McpServerStatusSnapshot,
    McpStatusSnapshot,
    McpStatusSource,
)
from pickel.extensions_host.registry import (
    AgentScope,
    ContributionLease,
    ContributionScope,
    ExtensionRegistry,
)

__all__ = [
    "AgentScope",
    "ConversationMode",
    "ContributionScope",
    "ContributionLease",
    "ConversationExtensionContext",
    "EventProcessor",
    "EventProcessorRegistration",
    "ExtensionConfigError",
    "ExtensionHost",
    "ExtensionLoadError",
    "ExtensionRegistry",
    "LoadResult",
    "extension_version_for_module",
    "McpServerStatusSnapshot",
    "McpStatusSnapshot",
    "McpStatusSource",
    "ResolvedEventProcessor",
    "load_extensions",
    "load_extensions_async",
    "teardown_extensions",
]
