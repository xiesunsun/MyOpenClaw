from datetime import datetime, timezone

from pickel.artifacts.artifact import ArtifactReference
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from pickel.conversations.content_blocks import (
    ArtifactBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from pickel.operations.agent_run_state import AgentRunError, Cancellation
from pickel.operations.delegation_result import (
    DEFAULT_DELEGATION_RESULT_MAX_CHARS,
    DelegationResultProjector,
    project_delegation_result,
    project_settled_message,
)

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
ARTIFACT = ArtifactReference(
    artifact_id="artifact_" + "a" * 64,
    media_type="text/plain",
    display_name="report.txt",
)


def test_success_projects_only_text_and_artifact_blocks() -> None:
    message = AssistantMessage(
        content=(
            ThinkingBlock("private reasoning"),
            TextBlock("one"),
            ToolCallBlock("tool-1", "secret_tool", {}),
            ArtifactBlock(ARTIFACT, alt_text="report"),
            TextBlock("two"),
        ),
        metadata=ModelResponseMetadata(
            provider="provider",
            model="model",
            provider_response_id="response-id",
            usage=ModelUsage(input_tokens=1, output_tokens=2),
        ),
    )

    result = project_delegation_result(status="succeeded", assistant_message=message)

    assert result == {
        "result": [
            {"type": "text", "text": "one"},
            {
                "type": "artifact",
                "artifact": {
                    "artifact_id": ARTIFACT.artifact_id,
                    "media_type": "text/plain",
                    "display_name": "report.txt",
                },
                "alt_text": "report",
            },
            {"type": "text", "text": "two"},
        ],
        "error": None,
    }


def test_text_budget_is_total_and_artifacts_are_not_truncated() -> None:
    message = AssistantMessage(
        content=(
            TextBlock("abc"),
            ArtifactBlock(ARTIFACT),
            TextBlock("def"),
        )
    )

    projector = DelegationResultProjector(max_chars=4)
    first = projector.project(status="succeeded", assistant_message=message)
    second = projector.project(status="succeeded", assistant_message=message)

    assert first == second
    assert first["result"] == [
        {"type": "text", "text": "abc"},
        {
            "type": "artifact",
            "artifact": {
                "artifact_id": ARTIFACT.artifact_id,
                "media_type": "text/plain",
                "display_name": "report.txt",
            },
            "alt_text": None,
        },
        {"type": "text", "text": "d"},
    ]
    assert first["truncated"] is True
    assert first["omitted_chars"] == 2


def test_small_result_keeps_legacy_shape() -> None:
    result = project_delegation_result(
        status="succeeded",
        assistant_message=AssistantMessage(content=(TextBlock("ok"),)),
    )
    assert result == {"result": [{"type": "text", "text": "ok"}], "error": None}


def test_failed_and_cancelled_results_are_stable_summaries() -> None:
    failed = project_delegation_result(
        status="failed",
        error=AgentRunError("provider_error", "请求失败", retryable=True),
    )
    cancelled = project_delegation_result(
        status="cancelled", cancellation=Cancellation("用户取消", NOW)
    )

    assert failed == {
        "result": None,
        "error": {
            "code": "provider_error",
            "message": "请求失败",
            "retryable": True,
        },
    }
    assert cancelled == {
        "result": None,
        "error": {
            "code": "cancelled",
            "message": "用户取消",
            "retryable": False,
        },
    }
    assert project_delegation_result(status="cancelled") == cancelled | {
        "error": {
            "code": "cancelled",
            "message": "child agent cancelled",
            "retryable": False,
        }
    }


def test_legacy_package_budget_defaults_to_8000() -> None:
    assert DEFAULT_DELEGATION_RESULT_MAX_CHARS == 8000
    assert DelegationResultProjector().max_chars == 8000


def test_settled_large_result_marks_preview_without_copying_content() -> None:
    message = AssistantMessage(content=(TextBlock("abcdef"),))
    settled = project_settled_message(
        child_session_id="child-1",
        status="succeeded",
        assistant_message=message,
        max_chars=3,
    )

    assert settled.content[0] == TextBlock(
        '{"child_session_id":"child-1","omitted_chars":3,"status":"succeeded",'
        '"truncated":true,"type":"agent_settled"}'
    )
    assert settled.content[1] == TextBlock("abc")
