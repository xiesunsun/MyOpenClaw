from __future__ import annotations

from typing import Any, Protocol

from myopenclaw.integrations.openviking.config import OpenVikingConfig


class OpenVikingContextClient(Protocol):
    def search(self, **kwargs: Any) -> Any: ...


class SyncHTTPOpenVikingContextClient:
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

    def search(self, **kwargs: Any) -> Any:
        return self._resolved_client().search(**kwargs)

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
                "OpenViking session recall is enabled, but the openviking package is not installed"
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
