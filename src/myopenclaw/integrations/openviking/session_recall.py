from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from myopenclaw.context import SessionRecallResult, SessionRecallSnippet
from myopenclaw.conversations.session import Session
from myopenclaw.integrations.openviking.config import OpenVikingConfig
from myopenclaw.integrations.openviking.context_client import OpenVikingContextClient


LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


class OpenVikingSessionRecallProvider:
    def __init__(
        self,
        *,
        config: OpenVikingConfig,
        client: OpenVikingContextClient,
    ) -> None:
        self._config = config
        self._client = client

    async def recall(
        self,
        *,
        session: Session,
        current_user_text: str,
    ) -> SessionRecallResult:
        recall_config = self._config.session_recall
        if (
            not self._config.enabled
            or not recall_config.enabled
            or not session.remote_session_id
        ):
            return SessionRecallResult()
        try:
            return await asyncio.to_thread(
                self._recall_sync,
                session.remote_session_id,
                current_user_text,
            )
        except Exception as exc:
            LOGGER.warning(
                "OpenViking session recall failed: %s",
                exc,
                exc_info=False,
            )
            return SessionRecallResult()

    def _recall_sync(
        self,
        remote_session_id: str,
        current_user_text: str,
    ) -> SessionRecallResult:
        recall_config = self._config.session_recall
        response = self._client.search(
            query=current_user_text,
            session_id=remote_session_id,
            limit=recall_config.limit,
        )
        return SessionRecallResult(
            snippets=self._extract_snippets(response=response, limit=recall_config.limit)
        )

    def _extract_snippets(
        self,
        *,
        response: Any,
        limit: int,
    ) -> list[SessionRecallSnippet]:
        candidates = [
            item for item in _iter_result_items(response) if _accept_item(item)
        ]
        candidates.sort(key=lambda item: _score(item) or 0.0, reverse=True)

        snippets: list[SessionRecallSnippet] = []
        for item in candidates:
            score = _score(item)
            min_score = self._config.session_recall.min_score
            if min_score is not None and (score is None or score < min_score):
                continue
            text = _choose_text(item)
            if not text:
                continue
            snippets.append(
                SessionRecallSnippet(
                    text=text,
                    source_uri=str(_get_value(item, "uri") or ""),
                    score=score,
                )
            )
            if len(snippets) >= limit:
                break
        return snippets


def _iter_result_items(response: Any) -> Iterable[Any]:
    if response is None:
        return []
    if isinstance(response, (list, tuple)):
        return response
    items: list[Any] = []
    if isinstance(response, dict):
        for key in ("resources", "memories", "results", "items", "documents"):
            value = response.get(key)
            if isinstance(value, list):
                items.extend(value)
        return items
    for key in ("resources", "memories", "results", "items", "documents"):
        value = getattr(response, key, None)
        if isinstance(value, list):
            items.extend(value)
    return items


def _accept_item(item: Any) -> bool:
    uri_value = _get_value(item, "uri")
    if not isinstance(uri_value, str) or not uri_value:
        return False
    if _is_directory(item, uri_value):
        return False
    name = PurePosixPath(uri_value).name
    return name not in {".overview.md", ".abstract.md", "profile.md"}


def _choose_text(item: Any) -> str:
    for field_name in ("abstract", "overview", "content", "text", "summary"):
        value = _get_value(item, field_name)
        if isinstance(value, str) and value.strip():
            return _collapse_text(value)
    return ""


def _collapse_text(text: str, *, limit: int = 1400) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 3]}..."


def _score(item: Any) -> float | None:
    value = _get_value(item, "score")
    if isinstance(value, int | float):
        return float(value)
    return None


def _get_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _is_directory(item: Any, uri: str) -> bool:
    if uri.endswith("/"):
        return True
    if _get_value(item, "is_dir") is True or _get_value(item, "is_directory") is True:
        return True
    kind = _get_value(item, "kind") or _get_value(item, "type")
    return isinstance(kind, str) and kind.lower() in {"directory", "dir", "folder"}
