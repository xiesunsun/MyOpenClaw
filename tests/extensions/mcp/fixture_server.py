"""测试用最小 stdio MCP server。测试以 sys.executable spawn 本文件。"""

import os
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Elicit, Resolve
from pydantic import BaseModel

mcp = MCPServer("fixture")


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


class UserDetails(BaseModel):
    name: str
    count: int


class ProjectDetails(BaseModel):
    project: str


async def request_user_details() -> Elicit[UserDetails]:
    return Elicit("Provide user details", UserDetails)


@mcp.tool()
def elicited(
    details: Annotated[UserDetails, Resolve(request_user_details)],
) -> str:
    """Return values collected through MCP 2.0 MRTR elicitation."""
    return f"elicited:{details.name}:{details.count}"


async def request_project_details(
    details: Annotated[UserDetails, Resolve(request_user_details)],
) -> Elicit[ProjectDetails]:
    return Elicit(f"Provide a project for {details.name}", ProjectDetails)


@mcp.tool()
def elicited_multi_round(
    project: Annotated[ProjectDetails, Resolve(request_project_details)],
) -> str:
    """Exercise two dependent MCP 2.0 input-required rounds."""
    return f"project:{project.project}"


if __name__ == "__main__":
    mcp.run()
