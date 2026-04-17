from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory


class PromptToolkitInputReader:
    """Single-line prompt_toolkit input with persistent in-process history."""

    def __init__(self) -> None:
        self._session: PromptSession[str] = PromptSession(history=InMemoryHistory())

    async def __call__(self, prompt: str) -> str:
        return await self._session.prompt_async(FormattedText([("bold ansicyan", prompt)]))
