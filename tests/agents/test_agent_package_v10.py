from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentPackageVersion,
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    SecretRef,
    WorkspacePolicy,
    canonical_json_bytes,
    decode_agent_package_content,
    decode_legacy_agent_package,
    package_version_id_for_content,
)
from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.config.app_config import AppConfig
from pickel.tools.bus import ToolBus


def _model() -> ModelVersion:
    return ModelVersion(
        provider="anthropic",
        model="claude-test",
        wire_protocol="anthropic-messages",
        api_base=None,
        temperature=None,
        max_input_tokens=None,
        max_output_tokens=1024,
        provider_options={"nested": {"items": ["x"]}},
        provider_implementation=ImplementationRef("provider", "anthropic-messages"),
        required_secret_refs=(SecretRef("providers.anthropic.api_key"),),
    )


def _content() -> dict:
    return {
        "format_version": 1,
        "agent_id": "Pickle",
        "behavior_instruction": "be useful",
        "model_policy": {
            "primary": {
                "provider": "anthropic",
                "model": "claude-test",
                "wire_protocol": "anthropic-messages",
                "api_base": None,
                "temperature": None,
                "max_input_tokens": None,
                "max_output_tokens": 1024,
                "provider_options": {"nested": {"items": ["x"]}},
                "provider_implementation": {
                    "kind": "provider",
                    "name": "anthropic-messages",
                    "version": None,
                    "digest": None,
                },
                "required_secret_refs": [{"name": "providers.anthropic.api_key"}],
            },
            "worker": None,
            "utility": None,
        },
        "runtime_policy": {
            "max_model_steps": 8,
            "context_turn_window": 5,
            "max_delegation_depth": 3,
        },
        "workspace_policy": {"file_scope": "workspace"},
        "skills": [],
        "tools": [],
        "extensions": [],
    }


def test_package_id_is_the_only_digest_and_created_at_is_not_content() -> None:
    content = _content()
    first = AgentPackageVersion(
        package_version_id=package_version_id_for_content(content),
        agent_id="Pickle",
        format_version=1,
        behavior_instruction="be useful",
        model_policy=ModelPolicy(primary=_model()),
        runtime_policy=AgentRuntimePolicy(8, 5, 3),
        workspace_policy=WorkspacePolicy(),
        skills=(),
        tools=(),
        extensions=(),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    second = AgentPackageVersion(
        **{
            **first.__dict__,
            "created_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
        }
    )
    assert first.package_version_id == second.package_version_id
    assert "digest" not in first.__dict__
    assert "definition" not in first.__dict__


def test_recursive_json_is_immutable() -> None:
    model = _model()
    with pytest.raises(TypeError):
        model.provider_options["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        model.provider_options["nested"]["items"] += ("y",)  # type: ignore[index]
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})


def test_target_codec_requires_canonical_id() -> None:
    content = _content()
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    assert loaded.model_policy.worker is None
    assert loaded.model_policy.utility is None
    with pytest.raises(ValueError, match="canonical"):
        decode_agent_package_content(
            package_version_id="agentpkg_" + "0" * 64,
            content=content,
            created_at=loaded.created_at,
        )


def test_legacy_codec_is_explicit_and_produces_target_shape() -> None:
    legacy = {
        "schema_version": 3,
        "agent_id": "Pickle",
        "definition": {"file_access_mode": "workspace"},
        "behavior_instruction": "be useful",
        "model": {
            "provider": "anthropic",
            "model": "claude-test",
            "api_base": None,
            "temperature": None,
            "max_input_tokens": None,
            "max_output_tokens": 1024,
            "provider_options": {},
            "required_secrets": ["api_key"],
        },
        "runtime": {"max_model_steps": 8, "context_turn_window": 5},
        "skills": [],
        "tools": [],
    }
    loaded = decode_legacy_agent_package(
        content=legacy,
        created_at=datetime.now(timezone.utc),
    )
    assert loaded.format_version == 1
    assert loaded.model_policy.primary.required_secret_refs == (
        SecretRef("providers.anthropic.api_key"),
    )
    assert not hasattr(loaded, "schema_version")


def test_builder_freezes_existing_app_config_without_role_fallback(
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agents" / "Pickle"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text("be useful", encoding="utf-8")
    config = AppConfig.model_validate(
        {
            "root": tmp_path,
            "default_agent": "Pickle",
            "default_llm": {"provider": "anthropic", "model": "claude-test"},
            "providers": {
                "anthropic": {
                    "models": {
                        "claude-test": {
                            "api_key": "secret",
                            "max_output_tokens": 1024,
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
    package = AgentPackageBuilder(
        app_config=config, tool_bus=ToolBus()
    ).build_agent_package_version()
    assert package.model_policy.worker is None
    assert package.model_policy.utility is None
    assert package.model_policy.primary.required_secret_refs == (
        SecretRef("providers.anthropic.api_key"),
    )
    assert "'api_key': 'secret'" not in str(package.content_dict())
