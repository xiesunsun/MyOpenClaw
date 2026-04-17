from __future__ import annotations

import unittest
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.agents.skills import SkillManifest
from myopenclaw.conversations.message import ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.conversations.session import Session
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.runs.context import AgentRuntimeContext
from myopenclaw.runs.context_usage import ContextUsageService
from myopenclaw.shared.generation import GenerateRequest, GenerateResult
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolSpec


class StubProvider(BaseLLMProvider):
    def __init__(
        self,
        *,
        request_estimates: dict[tuple[str | None, tuple[str, ...]], int | None] | None = None,
        request_estimate_sequences: dict[
            tuple[str | None, tuple[str, ...]],
            list[int | None],
        ]
        | None = None,
    ) -> None:
        self.request_estimates = request_estimates or {}
        self.request_estimate_sequences = {
            key: list(values)
            for key, values in (request_estimate_sequences or {}).items()
        }
        self.requests: list[GenerateRequest] = []

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        return cls()

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        raise AssertionError("generate should not be called")

    async def count_request_tokens(self, request: GenerateRequest) -> int | None:
        self.requests.append(request)
        key = (
            request.system_instruction,
            tuple(tool.name for tool in request.tools),
        )
        sequence = self.request_estimate_sequences.get(key)
        if sequence:
            return sequence.pop(0)
        return self.request_estimates.get(key)


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


class ContextUsageServiceTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, *, skills: list[SkillManifest] | None = None) -> Agent:
        return Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
                max_input_tokens=1048576,
            ),
            tool_ids=["echo"],
            skills=skills or [],
        )

    async def test_snapshot_counts_incremental_request_categories(self) -> None:
        skill = SkillManifest(
            name="excel",
            description="Analyze spreadsheets.",
            skill_dir=Path("/tmp/skills/excel"),
            skill_file=Path("/tmp/skills/excel/SKILL.md"),
        )
        agent = self._build_agent(skills=[skill])
        header_instruction = "\n\n".join(
            [
                agent.instruction_parts.base_instruction,
                agent.instruction_parts.skills_guidance,
                "Available skills:",
            ]
        )
        provider = StubProvider(
            request_estimates={
                (None, ()): 1200,
                (agent.instruction_parts.base_instruction, ()): 1500,
                (agent.system_instruction, ()): 2600,
                (agent.system_instruction, ("echo",)): 3200,
                (header_instruction, ()): 2100,
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("hello")

        snapshot = await ContextUsageService().build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )

        self.assertEqual(3200, snapshot.total_tokens)
        self.assertEqual(1200, snapshot.category("messages").token_count)
        self.assertEqual(300, snapshot.category("system").token_count)
        self.assertEqual(1100, snapshot.category("skills").token_count)
        self.assertEqual(600, snapshot.category("tools").token_count)
        self.assertEqual(500, snapshot.category("skills").details[0].token_count)
        self.assertEqual(1048576 - 3200, snapshot.free_tokens)

    async def test_snapshot_reuses_cached_result_when_session_hash_is_unchanged(self) -> None:
        agent = self._build_agent()
        provider = StubProvider(
            request_estimates={
                (None, ()): 100,
                (agent.instruction_parts.base_instruction, ()): 140,
                (agent.system_instruction, ()): 140,
                (agent.system_instruction, ("echo",)): 190,
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("hello")
        service = ContextUsageService()

        first = await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )
        request_count_after_first_build = len(provider.requests)
        second = await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )

        self.assertIs(first, second)
        self.assertEqual(request_count_after_first_build, len(provider.requests))

    async def test_snapshot_normalizes_empty_session_placeholder_tokens(self) -> None:
        skill = SkillManifest(
            name="excel",
            description="Analyze spreadsheets.",
            skill_dir=Path("/tmp/skills/excel"),
            skill_file=Path("/tmp/skills/excel/SKILL.md"),
        )
        agent = self._build_agent(skills=[skill])
        header_instruction = "\n\n".join(
            [
                agent.instruction_parts.base_instruction,
                agent.instruction_parts.skills_guidance,
                "Available skills:",
            ]
        )
        provider = StubProvider(
            request_estimates={
                (None, ()): 1,
                (agent.instruction_parts.base_instruction, ()): 90,
                (agent.system_instruction, ()): 422,
                (agent.system_instruction, ("echo",)): 1458,
                (header_instruction, ()): 354,
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])

        snapshot = await ContextUsageService().build(
            agent=agent,
            context=context,
            prompt_messages=[],
        )

        self.assertEqual(1457, snapshot.total_tokens)
        self.assertEqual(0, snapshot.category("messages").token_count)
        self.assertEqual(89, snapshot.category("system").token_count)
        self.assertEqual(332, snapshot.category("skills").token_count)
        self.assertEqual(1036, snapshot.category("tools").token_count)
        self.assertEqual(68, snapshot.category("skills").details[0].token_count)
        self.assertEqual(1048576 - 1457, snapshot.free_tokens)

    async def test_snapshot_recomputes_when_session_hash_changes(self) -> None:
        agent = self._build_agent()
        provider = StubProvider(
            request_estimates={
                (None, ()): 100,
                (agent.instruction_parts.base_instruction, ()): 140,
                (agent.system_instruction, ()): 140,
                (agent.system_instruction, ("echo",)): 190,
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("hello")
        service = ContextUsageService()

        await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )
        request_count_after_first_build = len(provider.requests)

        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="echo",
                        arguments={"text": "ping"},
                        thought_signature=b"sig-1",
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="pong",
                        metadata={"exit_code": 0},
                    )
                ],
            )
        )

        await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )

        self.assertGreater(len(provider.requests), request_count_after_first_build)

    async def test_snapshot_handles_provider_count_failures(self) -> None:
        agent = self._build_agent()
        provider = StubProvider()
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("hello")

        snapshot = await ContextUsageService().build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )

        self.assertIsNone(snapshot.total_tokens)
        self.assertIsNone(snapshot.category("system").token_count)
        self.assertIsNone(snapshot.category("skills").token_count)
        self.assertIsNone(snapshot.category("messages").token_count)
        self.assertIsNone(snapshot.category("tools").token_count)
        self.assertIsNone(snapshot.free_tokens)

    async def test_snapshot_uses_prompt_messages_when_provided(self) -> None:
        agent = self._build_agent()
        provider = StubProvider(
            request_estimates={
                (None, ()): 80,
                (agent.instruction_parts.base_instruction, ()): 110,
                (agent.system_instruction, ()): 110,
                (agent.system_instruction, ("echo",)): 160,
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("raw user")
        session.append_assistant_message("raw answer")

        prompt_messages = [session.messages[0]]
        snapshot = await ContextUsageService().build(
            agent=agent,
            context=context,
            prompt_messages=prompt_messages,
        )

        self.assertEqual(80, snapshot.category("messages").token_count)
        self.assertEqual(
            ["raw user", "raw user", "raw user", "raw user"],
            [request.messages[0].content for request in provider.requests[:4]],
        )
        self.assertTrue(all(len(request.messages) == 1 for request in provider.requests[:4]))

    async def test_snapshot_does_not_cache_failed_result(self) -> None:
        agent = self._build_agent()
        provider = StubProvider(
            request_estimate_sequences={
                (None, ()): [None, 100],
                (agent.instruction_parts.base_instruction, ()): [None, 140],
                (agent.system_instruction, ()): [None, 140],
                (agent.system_instruction, ("echo",)): [None, 190],
            }
        )
        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[EchoTool()])
        session = Session(session_id="session-1", agent_id=agent.agent_id)
        session.append_user_message("hello")
        service = ContextUsageService()

        first = await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )
        second = await service.build(
            agent=agent,
            context=context,
            prompt_messages=session.messages,
        )

        self.assertIsNone(first.total_tokens)
        self.assertEqual(190, second.total_tokens)
        self.assertIsNot(first, second)
        self.assertEqual(8, len(provider.requests))


if __name__ == "__main__":
    unittest.main()
