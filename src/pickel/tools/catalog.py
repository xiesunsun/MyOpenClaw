from pickel.tools.base import BaseTool
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.file_formatter import FileToolFormatter
from pickel.tools.file_tools import (
    EditTool,
    GlobTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
)
from pickel.tools.delegate_agent import delegate_agent
from pickel.tools.interrupt_agent import interrupt_agent
from pickel.tools.send_message import send_message
from pickel.tools.list_agents import list_agents
from pickel.tools.report import report
from pickel.tools.shell import BashTool


def builtin_tools() -> list[BaseTool]:
    formatter = FileToolFormatter()
    return [
        LsTool(formatter),
        GlobTool(formatter),
        GrepTool(formatter),
        ReadTool(formatter),
        EditTool(formatter),
        WriteTool(formatter),
        BashTool(),
        delegate_agent,
        send_message,
        list_agents,
        interrupt_agent,
        report,
    ]


def install_builtin_tools(bus: ToolBus) -> None:
    """把内置工具装进总线。重复调用幂等（同来源同 origin 覆盖）。"""
    for tool in builtin_tools():
        bus.register(tool, source=ToolSource.BUILTIN)
