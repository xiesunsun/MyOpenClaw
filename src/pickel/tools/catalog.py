from pickel.tools.base import BaseTool
from pickel.tools.builtin import echo, tool_set_active
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.file_formatter import FileToolFormatter
from pickel.tools.file_tools import (
    GlobSearchTool,
    GrepSearchTool,
    ListDirectoryTool,
    ReadFileTool,
    ReadManyFilesTool,
    ReplaceTool,
    WriteFileTool,
)
from pickel.tools.shell import (
    ShellCloseTool,
    ShellExecTool,
    ShellInterruptTool,
    ShellKillTool,
    ShellOutputTool,
    ShellRestartTool,
    ShellStdinTool,
    ShellTasksTool,
    ShellWaitTool,
)


def builtin_tools() -> list[BaseTool]:
    formatter = FileToolFormatter()
    return [
        echo,
        tool_set_active,
        ListDirectoryTool(formatter),
        GlobSearchTool(formatter),
        GrepSearchTool(formatter),
        ReadFileTool(formatter),
        ReadManyFilesTool(formatter),
        ReplaceTool(formatter),
        WriteFileTool(formatter),
        ShellExecTool(),
        ShellWaitTool(),
        ShellStdinTool(),
        ShellInterruptTool(),
        ShellTasksTool(),
        ShellOutputTool(),
        ShellKillTool(),
        ShellRestartTool(),
        ShellCloseTool(),
    ]


def install_builtin_tools(bus: ToolBus) -> None:
    """把内置工具装进总线。重复调用幂等（同来源同 origin 覆盖）。"""
    for tool in builtin_tools():
        bus.register(tool, source=ToolSource.BUILTIN)
