from pickel.tools.base import BaseTool
from pickel.tools.builtin import echo
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
from pickel.tools.shell import ShellCloseTool, ShellExecTool, ShellRestartTool


def builtin_tools() -> list[BaseTool]:
    formatter = FileToolFormatter()
    return [
        echo,
        ListDirectoryTool(formatter),
        GlobSearchTool(formatter),
        GrepSearchTool(formatter),
        ReadFileTool(formatter),
        ReadManyFilesTool(formatter),
        ReplaceTool(formatter),
        WriteFileTool(formatter),
        ShellExecTool(),
        ShellRestartTool(),
        ShellCloseTool(),
    ]
