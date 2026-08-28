from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentDelegationPolicy,
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
        delegation_policy=AgentDelegationPolicy("Pickle", ("Pickle",)),
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


def test_model_version_context_window_must_fit_output_budget() -> None:
    with pytest.raises(ValueError, match="context_window_tokens.*max_output_tokens"):
        ModelVersion(
            provider="anthropic",
            model="claude-test",
            wire_protocol="anthropic-messages",
            api_base=None,
            temperature=None,
            max_input_tokens=None,
            max_output_tokens=1024,
            context_window_tokens=1024,
            provider_options={},
            provider_implementation=ImplementationRef("provider", "anthropic-messages"),
            required_secret_refs=(),
        )


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


def test_v1_codec_keeps_legacy_hash_shape_and_uses_policy_defaults() -> None:
    content = _content()
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    assert loaded.content_dict() == content
    assert loaded.runtime_policy.model_request_max_attempts == 3
    assert loaded.runtime_policy.max_parallel_model_requests == 2
    assert loaded.runtime_policy.delegation_result_max_chars == 8000
    assert loaded.delegation_policy == AgentDelegationPolicy("Pickle", ("Pickle",))


def test_old_package_without_context_capacity_keeps_canonical_hash() -> None:
    content = _content()
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    assert loaded.model_policy.primary.context_window_tokens is None
    assert loaded.content_dict() == content


def test_package_codec_persists_context_capacity() -> None:
    content = _content()
    content["model_policy"]["primary"]["context_window_tokens"] = 1000000
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    assert loaded.model_policy.primary.context_window_tokens == 1000000
    assert loaded.model_policy.primary.effect_rate is None
    assert loaded.model_policy.primary.effective_input_token_limit() == 998976
    assert loaded.content_dict() == content


def test_package_codec_freezes_effect_rate_without_rewriting_old_packages() -> None:
    old_content = _content()
    old_content["model_policy"]["primary"]["context_window_tokens"] = 1000000
    old = decode_agent_package_content(
        package_version_id=package_version_id_for_content(old_content),
        content=old_content,
        created_at=datetime.now(timezone.utc),
    )
    assert old.model_policy.primary.effect_rate is None
    assert old.content_dict() == old_content

    new_content = _content()
    new_content["model_policy"]["primary"].update(
        {"context_window_tokens": 1000000, "effect_rate": 0.5}
    )
    new = decode_agent_package_content(
        package_version_id=package_version_id_for_content(new_content),
        content=new_content,
        created_at=datetime.now(timezone.utc),
    )
    assert new.model_policy.primary.effect_rate == 0.5
    assert new.model_policy.primary.effective_input_token_limit() == 498976
    assert new.content_dict() == new_content


def test_format_3_codec_freezes_delegation_policy_and_result_budget() -> None:
    content = _content()
    content["format_version"] = 3
    content["runtime_policy"].update(
        {
            "model_request_max_attempts": 3,
            "model_request_retry_initial_delay_ms": 1000,
            "model_request_retry_max_delay_ms": 4000,
            "max_parallel_model_requests": 2,
        }
    )
    content["runtime_policy"]["delegation_result_max_chars"] = 1234
    content["delegation_policy"] = {
        "default_agent_id": "Worker",
        "allowed_agent_ids": ["Pickle", "Worker"],
    }
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )

    assert loaded.format_version == 3
    assert loaded.runtime_policy.delegation_result_max_chars == 1234
    assert loaded.delegation_policy == AgentDelegationPolicy(
        "Worker", ("Pickle", "Worker")
    )
    assert loaded.content_dict() == content


def test_format_4_codec_freezes_retry_backoff_delays() -> None:
    content = _content()
    content["format_version"] = 4
    content["runtime_policy"].update(
        {
            "model_request_max_attempts": 3,
            "model_request_retry_delays_ms": [20000, 60000, 120000],
            "max_parallel_model_requests": 2,
            "delegation_result_max_chars": 1234,
        }
    )
    content["delegation_policy"] = {
        "default_agent_id": "Pickle",
        "allowed_agent_ids": ["Pickle"],
    }
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )

    assert loaded.format_version == 4
    assert loaded.runtime_policy.model_request_retry_delays_ms == (
        20000,
        60000,
        120000,
    )
    assert loaded.content_dict() == content


def test_format_3_hash_preserved_and_backoff_delays_synthesized() -> None:
    """旧冻结 Package 的 hash 必须逐字节复现，运行时退避按历史公式合成。"""
    content = _content()
    content["format_version"] = 3
    content["runtime_policy"].update(
        {
            "model_request_max_attempts": 3,
            "model_request_retry_initial_delay_ms": 1000,
            "model_request_retry_max_delay_ms": 4000,
            "max_parallel_model_requests": 2,
            "delegation_result_max_chars": 8000,
        }
    )
    content["delegation_policy"] = {
        "default_agent_id": "Pickle",
        "allowed_agent_ids": ["Pickle"],
    }
    loaded = decode_agent_package_content(
        package_version_id=package_version_id_for_content(content),
        content=content,
        created_at=datetime.now(timezone.utc),
    )

    assert loaded.format_version == 3
    assert loaded.content_dict() == content
    assert loaded.runtime_policy.model_request_retry_initial_delay_ms == 1000
    # 1s / 2s / 4s：与旧指数退避公式一致。
    assert loaded.runtime_policy.model_request_retry_delays_ms == (1000, 2000, 4000)


def test_runtime_policy_rejects_empty_or_negative_retry_delays() -> None:
    with pytest.raises(ValueError, match="model_request_retry_delays_ms"):
        AgentRuntimePolicy(max_model_steps=8, model_request_retry_delays_ms=())
    with pytest.raises(ValueError, match="model_request_retry_delays_ms"):
        AgentRuntimePolicy(max_model_steps=8, model_request_retry_delays_ms=(1000, -1))


def test_delegation_policy_requires_unique_non_empty_allowlist() -> None:
    with pytest.raises(ValueError, match="allowed_agent_ids"):
        AgentDelegationPolicy("Pickle", ())
    with pytest.raises(ValueError, match="去重"):
        AgentDelegationPolicy("Pickle", ("Pickle", "Pickle"))
    with pytest.raises(ValueError, match="default_agent_id"):
        AgentDelegationPolicy("Worker", ("Pickle",))


@pytest.mark.parametrize(
    "schema_value",
    [pytest.param(None, id="null"), pytest.param("missing", id="missing")],
)
def test_package_codec_rejects_missing_or_null_tool_output_schema(schema_value) -> None:
    content = _content()
    content["tools"] = [
        {
            "name": "echo",
            "source": "builtin",
            "implementation_ref": {
                "kind": "builtin",
                "name": "echo",
                "version": None,
                "digest": None,
            },
            "version": None,
            "description": "Echo",
            "input_schema": {"type": "object"},
            "replay_policy": "safe",
        }
    ]
    if schema_value != "missing":
        content["tools"][0]["output_schema"] = schema_value

    with pytest.raises((TypeError, ValueError), match="output_schema"):
        decode_agent_package_content(
            package_version_id=package_version_id_for_content(content),
            content=content,
            created_at=datetime.now(timezone.utc),
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

    legacy["tools"] = [{"name": "echo"}]
    with pytest.raises(ValueError, match="output_schema"):
        decode_legacy_agent_package(
            content=legacy,
            created_at=datetime.now(timezone.utc),
        )


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
    assert package.format_version == 4
    assert package.runtime_policy.model_request_max_attempts == 3
    assert package.runtime_policy.model_request_retry_delays_ms == (
        20000,
        60000,
        120000,
    )
    assert package.runtime_policy.max_parallel_model_requests == 2
    assert package.model_policy.worker is None
    assert package.model_policy.utility is None
    assert package.model_policy.primary.required_secret_refs == (
        SecretRef("providers.anthropic.api_key"),
    )
    assert "'api_key': 'secret'" not in str(package.content_dict())
