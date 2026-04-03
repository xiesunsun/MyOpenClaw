from myopenclaw.infrastructure.tools.base import BaseTool
from myopenclaw.infrastructure.tools.builtin import echo
from myopenclaw.infrastructure.tools.file_formatter import FileToolFormatter
from myopenclaw.infrastructure.tools.file_tools import (
    GlobSearchTool,
    GrepSearchTool,
    ListDirectoryTool,
    ReadFileTool,
    ReadManyFilesTool,
    ReplaceTool,
    WriteFileTool,
)
from myopenclaw.infrastructure.tools.shell import ShellCloseTool, ShellExecTool, ShellRestartTool


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
