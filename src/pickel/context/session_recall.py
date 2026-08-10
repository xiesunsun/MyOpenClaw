from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pickel.conversations.session import Session


@dataclass(frozen=True)
class SessionRecallSnippet:
    text: str
    source_uri: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class SessionRecallResult:
    snippets: list[SessionRecallSnippet] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.snippets


class SessionRecallProvider(Protocol):
    async def recall(
        self,
        *,
        session: Session,
        current_user_text: str,
    ) -> SessionRecallResult: ...


def render_session_recall(
    result: SessionRecallResult | None,
    *,
    max_chars: int | None = None,
) -> str | None:
    if result is None or result.is_empty:
        return None

    snippets = [snippet for snippet in result.snippets if snippet.text.strip()]
    rendered = _render_snippets(snippets)
    if rendered is None:
        return None
    if max_chars is None or len(rendered) <= max_chars:
        return rendered

    while snippets:
        snippets.pop()
        rendered = _render_snippets(snippets)
        if rendered is None:
            return None
        if len(rendered) <= max_chars:
            return rendered
    return None


def _render_snippets(snippets: list[SessionRecallSnippet]) -> str | None:
    if not snippets:
        return None

    sections = [
        f"[{index}]\n{snippet.text.strip()}"
        for index, snippet in enumerate(snippets, start=1)
    ]
    return (
        "<Session_Retrieved_Context>\n"
        "The following is retrieved conversation/session context related to the next user message.\n"
        "It is not a new user request. Use it only as background.\n\n"
        "Recent or relevant session context:\n\n"
        + "\n\n".join(sections)
        + "\n</Session_Retrieved_Context>"
    )
