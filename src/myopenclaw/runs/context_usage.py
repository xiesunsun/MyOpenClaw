from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, field

from myopenclaw.agents.agent import Agent
from myopenclaw.agents.skills import SystemInstructionParts, format_skill_catalog_entry
from myopenclaw.conversations.message import SessionMessage
from myopenclaw.runs.context import AgentRuntimeContext
from myopenclaw.shared.generation import GenerateRequest


@dataclass(frozen=True)
class ContextUsageDetail:
    label: str
    token_count: int | None


@dataclass(frozen=True)
class ContextUsageCategory:
    key: str
    label: str
    token_count: int | None
    char_count: int | None = None
    details: list[ContextUsageDetail] = field(default_factory=list)


@dataclass(frozen=True)
class ContextUsageSnapshot:
    model_label: str
    max_input_tokens: int | None
    total_tokens: int | None
    categories: list[ContextUsageCategory]
    free_tokens: int | None

    def category(self, key: str) -> ContextUsageCategory:
        for category in self.categories:
            if category.key == key:
                return category
        raise KeyError(key)


@dataclass(frozen=True)
class _ContextUsageCacheEntry:
    fingerprint: str
    snapshot: ContextUsageSnapshot


class ContextUsageService:
    def __init__(self) -> None:
        self._cached_prompt_messages_hash: str | None = None
        self._cached_entry: _ContextUsageCacheEntry | None = None

    async def build(
        self,
        *,
        agent: Agent,
        context: AgentRuntimeContext,
        prompt_messages: list[SessionMessage],
    ) -> ContextUsageSnapshot:
        instruction_parts = agent.instruction_parts
        messages = list(prompt_messages)
        prompt_messages_hash = self._hash_prompt_messages(messages=messages)
        fingerprint = self._cache_fingerprint(
            agent=agent,
            context=context,
            prompt_messages_hash=prompt_messages_hash,
        )
        if (
            self._cached_prompt_messages_hash == prompt_messages_hash
            and self._cached_entry is not None
            and self._cached_entry.fingerprint == fingerprint
        ):
            return self._cached_entry.snapshot

        is_empty_session = not messages
        full_system_instruction = instruction_parts.full_instruction or None
        messages_only_request = GenerateRequest(
            system_instruction=None,
            messages=messages,
            tools=[],
        )
        base_system_request = GenerateRequest(
            system_instruction=instruction_parts.base_instruction or None,
            messages=messages,
            tools=[],
        )
        full_system_request = GenerateRequest(
            system_instruction=full_system_instruction,
            messages=messages,
            tools=[],
        )
        full_request = GenerateRequest(
            system_instruction=full_system_instruction,
            messages=messages,
            tools=[tool.spec for tool in context.tools],
        )
        (
            messages_only_tokens,
            base_system_tokens,
            full_system_tokens,
            full_request_tokens,
        ) = await asyncio.gather(
            context.provider.count_request_tokens(messages_only_request),
            context.provider.count_request_tokens(base_system_request),
            context.provider.count_request_tokens(full_system_request),
            context.provider.count_request_tokens(full_request),
        )
        normalization_offset = messages_only_tokens if is_empty_session else 0

        snapshot = ContextUsageSnapshot(
            model_label=f"{agent.model_config.provider} / {agent.model_config.model}",
            max_input_tokens=agent.model_config.max_input_tokens,
            total_tokens=self._subtract_offset(full_request_tokens, normalization_offset),
            categories=[
                ContextUsageCategory(
                    key="system",
                    label="System prompt",
                    token_count=self._delta(base_system_tokens, messages_only_tokens),
                ),
                ContextUsageCategory(
                    key="skills",
                    label="Skills",
                    token_count=self._delta(full_system_tokens, base_system_tokens),
                    details=await self._build_skill_usage_details(
                        agent=agent,
                        context=context,
                        messages=messages,
                        instruction_parts=instruction_parts,
                        full_system_tokens=full_system_tokens,
                    ),
                ),
                ContextUsageCategory(
                    key="messages",
                    label="Messages",
                    token_count=self._subtract_offset(messages_only_tokens, normalization_offset),
                ),
                ContextUsageCategory(
                    key="session_recall",
                    label="Session recall message",
                    token_count=None,
                    char_count=(
                        len(context.last_session_recall_message.content)
                        if context.last_session_recall_message is not None
                        else 0
                    ),
                ),
                ContextUsageCategory(
                    key="tools",
                    label="Tools",
                    token_count=self._delta(full_request_tokens, full_system_tokens),
                ),
            ],
            free_tokens=(
                agent.model_config.max_input_tokens - self._subtract_offset(full_request_tokens, normalization_offset)
                if agent.model_config.max_input_tokens is not None
                and self._subtract_offset(full_request_tokens, normalization_offset) is not None
                else None
            ),
        )

        if self._is_cacheable(snapshot):
            self._cached_prompt_messages_hash = prompt_messages_hash
            self._cached_entry = _ContextUsageCacheEntry(
                fingerprint=fingerprint,
                snapshot=snapshot,
            )
        else:
            self._cached_prompt_messages_hash = None
            self._cached_entry = None
        return snapshot

    @staticmethod
    async def _build_skill_usage_details(
        *,
        agent: Agent,
        context: AgentRuntimeContext,
        messages: list[SessionMessage],
        instruction_parts: SystemInstructionParts,
        full_system_tokens: int | None,
    ) -> list[ContextUsageDetail]:
        if not agent.skills:
            return []

        catalog_lines = ["Available skills:"]
        baseline_instruction = ContextUsageService._join_instruction_sections(
            instruction_parts.base_instruction,
            instruction_parts.skills_guidance,
            "\n".join(catalog_lines),
        )
        baseline_count = await context.provider.count_request_tokens(
            GenerateRequest(
                system_instruction=baseline_instruction or None,
                messages=messages,
                tools=[],
            )
        )

        intermediate_requests: list[GenerateRequest] = []
        for skill in agent.skills[:-1]:
            catalog_lines.append(format_skill_catalog_entry(skill))
            intermediate_requests.append(
                GenerateRequest(
                    system_instruction=ContextUsageService._join_instruction_sections(
                        instruction_parts.base_instruction,
                        instruction_parts.skills_guidance,
                        "\n".join(catalog_lines),
                    )
                    or None,
                    messages=messages,
                    tools=[],
                )
            )

        cumulative_counts = (
            list(
                await asyncio.gather(
                    *(
                        context.provider.count_request_tokens(request)
                        for request in intermediate_requests
                    )
                )
            )
            if intermediate_requests
            else []
        )
        cumulative_counts.append(full_system_tokens)

        details: list[ContextUsageDetail] = []
        previous_count = baseline_count
        for skill, current_count in zip(agent.skills, cumulative_counts):
            details.append(
                ContextUsageDetail(
                    label=skill.name,
                    token_count=ContextUsageService._delta(current_count, previous_count),
                )
            )
            previous_count = current_count
        return details

    @staticmethod
    def _join_instruction_sections(*sections: str) -> str:
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _hash_prompt_messages(*, messages: list[SessionMessage]) -> str:
        payload = [ContextUsageService._serialize_session_message(message) for message in messages]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _cache_fingerprint(
        *,
        agent: Agent,
        context: AgentRuntimeContext,
        prompt_messages_hash: str,
    ) -> str:
        payload = {
            "agent_id": agent.agent_id,
            "model": {
                "provider": agent.model_config.provider,
                "name": agent.model_config.model,
            },
            "prompt_messages_hash": prompt_messages_hash,
            "system_instruction": agent.system_instruction,
            "tools": [
                {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "input_schema": tool.spec.input_schema,
                    "output_schema": tool.spec.output_schema,
                }
                for tool in context.tools
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _serialize_session_message(message: SessionMessage) -> dict[str, object]:
        payload: dict[str, object] = {
            "role": message.role.value,
            "content": message.content,
        }
        if message.provider_thinking_blocks is not None:
            payload["provider_thinking_blocks"] = [
                dict(block) for block in message.provider_thinking_blocks
            ]
        batch = message.tool_call_batch
        if batch is None:
            return payload

        payload["tool_call_batch"] = {
            "batch_id": batch.batch_id,
            "step_index": batch.step_index,
            "calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "thought_signature": (
                        base64.b64encode(call.thought_signature).decode("ascii")
                        if call.thought_signature is not None
                        else None
                    ),
                }
                for call in batch.calls
            ],
            "results": [
                {
                    "call_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                    "metadata": result.metadata,
                }
                for result in batch.results
            ],
        }
        return payload

    @staticmethod
    def _delta(upper: int | None, lower: int | None) -> int | None:
        if upper is None or lower is None:
            return None
        return upper - lower

    @staticmethod
    def _subtract_offset(value: int | None, offset: int | None) -> int | None:
        if value is None:
            return None
        if offset in (None, 0):
            return value
        return value - offset

    @staticmethod
    def _is_cacheable(snapshot: ContextUsageSnapshot) -> bool:
        if snapshot.total_tokens is None or snapshot.free_tokens is None:
            return False
        for category in snapshot.categories:
            if category.token_count is None and category.char_count is None:
                return False
            if any(detail.token_count is None for detail in category.details):
                return False
        return True
