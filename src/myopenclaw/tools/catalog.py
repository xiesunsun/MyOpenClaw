from myopenclaw.tools.base import BaseTool
from myopenclaw.tools.builtin import echo
from myopenclaw.tools.filesystem import ReadTool, WriteTool
from myopenclaw.tools.shell import ShellCloseTool, ShellExecTool, ShellRestartTool


def builtin_tools() -> list[BaseTool]:
    return [echo, ReadTool(), WriteTool(), ShellExecTool(), ShellRestartTool(), ShellCloseTool()]
