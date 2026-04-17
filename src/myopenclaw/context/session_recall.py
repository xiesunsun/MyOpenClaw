from __future__ import annotations

from typing import Protocol

from myopenclaw.context.models import SessionRecallResult, SessionRecallSnippet
from myopenclaw.conversations.message import MessageRole, SessionMessage
from myopenclaw.conversations.session import Session


class SessionRecallProvider(Protocol):
    async def recall(
        self,
        *,
        session: Session,
        current_user_text: str,
    ) -> SessionRecallResult: ...


class NoopSessionRecallProvider:
    async def recall(
        self,
        *,
        session: Session,
        current_user_text: str,
    ) -> SessionRecallResult:
        return SessionRecallResult()


def build_session_recall_message(
    result: SessionRecallResult | None,
    *,
    max_chars: int | None = None,
) -> SessionMessage | None:
    rendered = render_session_recall(result, max_chars=max_chars)
    if rendered is None:
        return None
    return SessionMessage(role=MessageRole.USER, content=rendered)


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
