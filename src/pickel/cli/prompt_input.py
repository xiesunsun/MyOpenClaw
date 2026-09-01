from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory


class PromptToolkitInputReader:
    """Single-line prompt_toolkit input with persistent in-process history."""

    def __init__(self) -> None:
        self._session: PromptSession[str] = PromptSession(history=InMemoryHistory())

    def set_completer(self, completer: Completer) -> None:
        """装配动态 Slash 补全；保留无参数构造以兼容自定义输入端。"""
        self._session.completer = completer
        self._session.complete_while_typing = True

    async def __call__(self, prompt: str) -> str:
        return await self._session.prompt_async(
            FormattedText([("bold ansicyan", prompt)])
        )
