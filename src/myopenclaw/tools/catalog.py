from myopenclaw.tools.base import BaseTool
from myopenclaw.tools.builtin import echo, read_file


def builtin_tools() -> list[BaseTool]:
    return [echo, read_file]
