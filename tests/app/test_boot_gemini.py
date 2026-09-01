"""Gemini Boot 与统一 PreparedModelCall Runtime 管道合同。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pickel.app.boot import Boot
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.config.app_config import AppConfig
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.providers.gemini import GeminiProvider
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.shared.execution_identity import ExecutionIdentity


def _config(tmp_path) -> AppConfig:
    agent_dir = tmp_path / "agents" / "Pickle"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text("You are Pickle.\n", encoding="utf-8")
    return AppConfig.model_validate(
        {
            "root": tmp_path,
            "default_agent": "Pickle",
            "default_llm": {"provider": "google/gemini", "model": "gemini-test"},
            "providers": {
                "google/gemini": {
                    "models": {
                        "gemini-test": {
                            "api_key": "test-key",
                            "wire_protocol": "gemini-generate-content",
                        }
                    }
                }
            },
            "agents": {
                "Pickle": {
                    "workspace_path": ".",
                    "behavior_path": "agents/Pickle",
                    "tools": [],
                    "extensions": [],
                }
            },
        }
    )


def _artifact_service() -> ArtifactService:
    store = InMemoryRuntimeStore()
    return ArtifactService(artifact_store=store, blob_store=InMemoryBlobStore())


def test_boot_accepts_gemini_and_freezes_it_in_loaded_package(
    tmp_path, monkeypatch
) -> None:
    sentinel_client = SimpleNamespace()
    monkeypatch.setattr(
        "pickel.providers.gemini.genai.Client", lambda **_: sentinel_client
    )
    artifact_service = _artifact_service()

    loaded = Boot.from_config(_config(tmp_path)).resolve_loaded_agent_package(
        artifact_service=artifact_service
    )

    provider = loaded.model_clients["primary"]
    assert isinstance(provider, GeminiProvider)
    assert provider.artifact_service is artifact_service
    assert loaded.version.model_policy.primary.wire_protocol == (
        "gemini-generate-content"
    )


def test_gemini_uses_prepared_runtime_send_path() -> None:
    provider = GeminiProvider(model="gemini-test", api_key="test-key")
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(
                    return_value=SimpleNamespace(
                        candidates=[
                            SimpleNamespace(
                                content=SimpleNamespace(
                                    parts=[
                                        SimpleNamespace(
                                            text="hello", function_call=None
                                        )
                                    ]
                                ),
                                finish_message=None,
                            )
                        ],
                        response_id="response-1",
                        model_version="gemini-test-v1",
                        usage_metadata=None,
                        text="hello",
                        function_calls=None,
                    )
                )
            )
        )
    )
    context = ModelContext(
        system=SystemContent.from_text("system"),
        messages=[UserMessage(content=[TextBlock(text="hi")])],
    )
    prepared = provider.prepare(context)
    results = asyncio.run(
        RuntimeEffects(provider=provider).execute_prepared_model_call(
            prepared=prepared,
            identity=ExecutionIdentity(
                session_id="session-1",
                operation_id="operation-1",
                step_id="step-1",
                step_sequence=1,
            ),
        )
    )

    assert results.assistant_message.content[0].text == "hello"
    assert results.provider_response["response_id"] == "response-1"
    provider.client.aio.models.generate_content.assert_awaited_once()


def test_gemini_prepared_request_resolves_artifact_through_service() -> None:
    artifact_service = _artifact_service()
    reference = artifact_service.create_artifact(
        data=b"png-bytes", media_type="image/png"
    )
    provider = GeminiProvider(
        model="gemini-test",
        api_key="test-key",
        artifact_service=artifact_service,
    )

    prepared = provider.prepare(
        ModelContext(
            system=SystemContent(),
            messages=[UserMessage(content=[ArtifactBlock(artifact=reference)])],
        )
    )

    inline_data = prepared.body["contents"][0]["parts"][0]["inlineData"]
    assert inline_data["mimeType"] == "image/png"
    assert inline_data["data"] == "cG5nLWJ5dGVz"
