from __future__ import annotations

from typing import Any, Protocol

from myopenclaw.integrations.openviking.config import OpenVikingConfig


class OpenVikingSessionClient(Protocol):
    def ensure_session(self, *, session_id: str) -> str: ...

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str | None = None,
        parts: list[dict] | None = None,
    ) -> None: ...

    def commit_session(self, *, session_id: str) -> None: ...

    def delete_session(self, *, session_id: str) -> None: ...


class SyncHTTPOpenVikingSessionClient:
    def __init__(
        self,
        config: OpenVikingConfig,
        *,
        remote_agent_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._remote_agent_id = remote_agent_id
        self._client = client
        self._initialized = False

    def ensure_session(self, *, session_id: str) -> str:
        try:
            response = self._call_session_method("get_session", session_id=session_id)
            return _session_id_from_response(response, fallback=session_id)
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        response = self._call_session_method("create_session", session_id=session_id)
        return _session_id_from_response(response, fallback=session_id)

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str | None = None,
        parts: list[dict] | None = None,
    ) -> None:
        self._call_session_method(
            "add_message",
            session_id=session_id,
            role=role,
            content=content,
            parts=parts,
        )

    def commit_session(self, *, session_id: str) -> None:
        self._call_session_method("commit_session", session_id=session_id)

    def delete_session(self, *, session_id: str) -> None:
        try:
            self._call_session_method("delete_session", session_id=session_id)
        except Exception as exc:
            if not _is_not_found(exc):
                raise

    def _call_session_method(self, method_name: str, **kwargs: Any) -> Any:
        target = self._resolved_client()
        sessions = getattr(target, "sessions", None)
        method_names = _method_aliases(method_name)
        for name in method_names:
            if hasattr(target, name):
                return getattr(target, name)(**kwargs)
        if sessions is not None:
            for name in method_names:
                if hasattr(sessions, name):
                    return getattr(sessions, name)(**kwargs)
        raise AttributeError(f"OpenViking client does not expose {method_name}")

    @staticmethod
    def _build_sdk_client(
        config: OpenVikingConfig,
        *,
        remote_agent_id: str | None = None,
    ) -> Any:
        try:
            from openviking import SyncHTTPClient
        except ImportError as exc:
            raise RuntimeError(
                "OpenViking sync is enabled, but the openviking package is not installed"
            ) from exc
        return SyncHTTPClient(
            url=config.base_url,
            api_key=config.user_key,
            agent_id=remote_agent_id,
            account=config.account_id,
            user=config.user_id,
            timeout=config.timeout_seconds,
        )

    def _resolved_client(self) -> Any:
        if self._client is None:
            self._client = self._build_sdk_client(
                self._config,
                remote_agent_id=self._remote_agent_id,
            )
        if not self._initialized and hasattr(self._client, "initialize"):
            self._client.initialize()
            self._initialized = True
        return self._client


def _session_id_from_response(response: Any, *, fallback: str) -> str:
    if response is None:
        return fallback
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return str(response.get("session_id") or response.get("id") or fallback)
    return str(
        getattr(response, "session_id", None)
        or getattr(response, "id", None)
        or fallback
    )


def _is_not_found(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 404:
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 404:
        return True
    return "not found" in str(exc).lower()


def _method_aliases(method_name: str) -> list[str]:
    aliases = {
        "add_message": ["add_message", "append_message"],
        "commit_session": ["commit_session", "commit"],
    }
    names = aliases.get(method_name, [method_name])
    short_name = method_name.removesuffix("_session")
    if short_name not in names:
        names.append(short_name)
    return names
