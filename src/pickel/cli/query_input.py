"""Unix query 输入到 Conversation UserMessage 的唯一适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent


@dataclass(frozen=True)
class QueryInput:
    query: str
    stdin_text: str | None = None

    def to_user_message(self) -> UserMessage:
        if self.query == "-":
            if self.stdin_text is None:
                raise ValueError("query 为 '-' 时必须通过 stdin 提供用户输入")
            if not self.stdin_text.strip():
                raise ValueError("stdin 用户输入不能为空")
            return UserMessage(content=[TextContent(text=self.stdin_text)])

        if not self.query.strip():
            raise ValueError("query 不能为空")
        if self.stdin_text is None or not self.stdin_text:
            return UserMessage(content=[TextContent(text=self.query)])
        return UserMessage(
            content=[
                TextContent(text=f"任务：\n{self.query}"),
                TextContent(text=f"输入数据（stdin）：\n{self.stdin_text}"),
            ]
        )


def read_query_input(query: str, stdin: TextIO) -> QueryInput:
    stdin_text = None if stdin.isatty() else stdin.read()
    if stdin_text == "":
        stdin_text = None
    return QueryInput(query=query, stdin_text=stdin_text)
