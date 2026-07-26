"""测试用最小 stdio MCP server。测试以 sys.executable spawn 本文件。"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back."""
    return f"echo:{text}"


@mcp.tool()
def boom() -> str:
    """Always fails with an error."""
    raise RuntimeError("boom")


@mcp.tool()
def die() -> str:
    """Exit the server process immediately (for reconnect tests)."""
    os._exit(1)


if __name__ == "__main__":
    mcp.run()
