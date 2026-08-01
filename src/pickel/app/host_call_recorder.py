"""把允许审计的 Host call 投影为 SessionEntry。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pickel.conversations.service import SessionService
from pickel.conversations.session import Session
from pickel.runs.host_calls import (
    HostCallCompleted,
    HostCallContext,
    HostCallOutcome,
    HostCallSpec,
)
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    EXTERNAL_ACTION_CALL,
    STRUCTURED_INPUT_CALL,
)

_RECORDED_CALLS = {
    CONFIRMATION_CALL.key,
    STRUCTURED_INPUT_CALL.key,
    EXTERNAL_ACTION_CALL.key,
}


class SessionHostCallRecorder:
    """当前 Session 的审计适配器；Router 本身不认识持久层。"""

    def __init__(
        self,
        *,
        session: Session,
        session_service: SessionService | Any | None,
    ) -> None:
        self._session = session
        self._session_service = session_service

    def record_started(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
    ) -> None:
        if spec.key not in _RECORDED_CALLS:
            return
        request_payload = _json_value(request)
        if spec.key == EXTERNAL_ACTION_CALL.key and isinstance(request_payload, dict):
            url = request_payload.get("url")
            if isinstance(url, str):
                request_payload["url"] = _url_without_query_or_fragment(url)
        entry = self._session.append_host_call_request(
            {
                "call": {"name": spec.name, "version": spec.version},
                "context": asdict(context),
                "request": request_payload,
            }
        )
        self._flush(entry)

    def record_finished(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
        outcome: HostCallOutcome[Any],
    ) -> None:
        if spec.key not in _RECORDED_CALLS:
            return
        payload: dict[str, Any] = {
            "call": {"name": spec.name, "version": spec.version},
            "call_id": context.call_id,
            "outcome": _outcome_name(outcome),
        }
        if isinstance(outcome, HostCallCompleted):
            payload["response"] = _json_value(outcome.value)
        else:
            payload["detail"] = _json_value(outcome)
        entry = self._session.append_host_call_response(payload)
        self._flush(entry)

    def _flush(self, entry: Any) -> None:
        if self._session_service is None:
            return
        self._session_service.flush_new_entries(
            session=self._session,
            entries=[entry],
        )


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _outcome_name(outcome: HostCallOutcome[Any]) -> str:
    name = type(outcome).__name__
    prefix = "HostCall"
    if name.startswith(prefix):
        name = name[len(prefix) :]
    return name.removesuffix("Outcome").lower()


def _url_without_query_or_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
