from myopenclaw.tools.base import BaseTool
from myopenclaw.tools.builtin import echo
from myopenclaw.tools.file_formatter import FileToolFormatter
from myopenclaw.tools.file_tools import (
    GlobSearchTool,
    GrepSearchTool,
    ListDirectoryTool,
    ReadFileTool,
    ReadManyFilesTool,
    ReplaceTool,
    WriteFileTool,
)
from myopenclaw.tools.shell import ShellCloseTool, ShellExecTool, ShellRestartTool


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
