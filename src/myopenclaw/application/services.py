from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from uuid import uuid4

from myopenclaw.application.contracts import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    ModelMessage,
    ModelMessageRole,
    ModelToolCall,
    ModelToolCallBatch,
    ModelToolResult,
    TurnResult,
)
from myopenclaw.application.events import (
    RuntimeEvent,
    RuntimeEventHandler,
    RuntimeEventType,
    ToolCallView,
    ToolResultView,
)
from myopenclaw.application.ports import LLMPort, ToolExecutorPort
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.message import ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.domain.metadata import MessageMetadata
from myopenclaw.domain.session import Session


@dataclass
class ChatService:
    agent: Agent
    session: Session
    llm: LLMPort
    tool_executor: ToolExecutorPort
    max_steps: int = 8

    async def run_turn(
        self,
        text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> TurnResult:
        self.session.append_user_message(text)
        tool_batches: list[ModelToolCallBatch] = []
        last_metadata: MessageMetadata | None = None

        for step_index in range(1, self.max_steps + 1):
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=step_index,
                ),
            )
            start = time.perf_counter()
            result = await self.llm.generate(
                GenerateRequest(
                    system_instruction=self.agent.system_instruction or None,
                    messages=self._build_model_messages(),
                    tools=self.tool_executor.describe_tools(self.agent.tool_ids),
                )
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            metadata = result.metadata or MessageMetadata(
                provider=self.agent.model_provider,
                model=self.agent.model_name,
                input_tokens=result.usage.input_tokens if result.usage else None,
                output_tokens=result.usage.output_tokens if result.usage else None,
                total_tokens=result.usage.total_tokens if result.usage else None,
                elapsed_ms=elapsed_ms,
                provider_finish_reason=result.provider_finish_reason,
                provider_finish_message=result.provider_finish_message,
                provider_response_id=result.provider_response_id,
                provider_model_version=result.provider_model_version,
            )
            last_metadata = metadata

            if result.tool_calls:
                batch = await self._handle_tool_calls(
                    step_index=step_index,
                    tool_calls=result.tool_calls,
                    content=result.text,
                    metadata=metadata,
                    event_handler=event_handler,
                )
                tool_batches.append(batch)
                continue

            self.session.append_assistant_message(result.text, metadata=metadata)
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    step_index=step_index,
                    text=result.text,
                    metadata=metadata,
                ),
            )
            return TurnResult(
                text=result.text,
                metadata=metadata,
                finish_reason=result.finish_reason,
                tool_batches=tool_batches,
                message_count=len(self.session.messages),
            )

        message = "Reached the maximum number of reasoning steps."
        self.session.append_assistant_message(message, metadata=last_metadata)
        await self._emit_event(
            event_handler,
            RuntimeEvent(
                event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                step_index=self.max_steps,
                text=message,
                metadata=last_metadata,
            ),
        )
        return TurnResult(
            text=message,
            metadata=last_metadata,
            finish_reason=FinishReason.MAX_STEPS,
            tool_batches=tool_batches,
            message_count=len(self.session.messages),
        )

    def _build_model_messages(self) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for message in self.session.messages:
            if message.role.value == "user":
                messages.append(
                    ModelMessage(
                        role=ModelMessageRole.USER,
                        content=message.content,
                    )
                )
                continue

            batch = None
            if message.tool_call_batch is not None:
                batch = ModelToolCallBatch(
                    batch_id=message.tool_call_batch.batch_id,
                    step_index=message.tool_call_batch.step_index,
                    calls=[
                        ModelToolCall(
                            id=tool_call.id,
                            name=tool_call.name,
                            arguments=dict(tool_call.arguments),
                            thought_signature=tool_call.thought_signature,
                        )
                        for tool_call in message.tool_call_batch.calls
                    ],
                    results=[
                        ModelToolResult(
                            call_id=result.call_id,
                            content=result.content,
                            is_error=result.is_error,
                            metadata=dict(result.metadata),
                        )
                        for result in message.tool_call_batch.results
                    ],
                )
            messages.append(
                ModelMessage(
                    role=ModelMessageRole.ASSISTANT,
                    content=message.content,
                    tool_call_batch=batch,
                )
            )
        return messages

    async def _handle_tool_calls(
        self,
        *,
        step_index: int,
        tool_calls: list[ModelToolCall],
        content: str,
        metadata: MessageMetadata,
        event_handler: RuntimeEventHandler | None,
    ) -> ModelToolCallBatch:
        batch_id = uuid4().hex
        domain_calls = [
            ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=dict(tool_call.arguments),
                thought_signature=tool_call.thought_signature,
            )
            for tool_call in tool_calls
        ]

        total_calls = len(domain_calls)
        for call_index, tool_call in enumerate(domain_calls):
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    step_index=step_index,
                    batch_id=batch_id,
                    call_index=call_index,
                    total_calls=total_calls,
                    tool_call=ToolCallView(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=dict(tool_call.arguments),
                    ),
                ),
            )

        results = await self.tool_executor.execute_calls(
            tool_calls=domain_calls,
            agent=self.agent,
            session_id=self.session.session_id,
        )
        batch = ToolCallBatch(
            batch_id=batch_id,
            step_index=step_index,
            calls=domain_calls,
            results=[
                ToolCallResult(
                    call_id=result.call_id,
                    content=result.content,
                    is_error=result.is_error,
                    metadata=dict(result.metadata),
                )
                for result in results
            ],
        )
        self.session.append_assistant_tool_batch(
            batch=batch,
            content=content,
            metadata=metadata,
        )
        model_batch = ModelToolCallBatch(
            batch_id=batch_id,
            step_index=step_index,
            calls=[
                ModelToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    thought_signature=tool_call.thought_signature,
                )
                for tool_call in domain_calls
            ],
            results=[
                ModelToolResult(
                    call_id=result.call_id,
                    content=result.content,
                    is_error=result.is_error,
                    metadata=dict(result.metadata),
                )
                for result in results
            ],
        )

        for call_index, (tool_call, result) in enumerate(zip(domain_calls, results)):
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=(
                        RuntimeEventType.TOOL_CALL_FAILED
                        if result.is_error
                        else RuntimeEventType.TOOL_CALL_COMPLETED
                    ),
                    step_index=step_index,
                    batch_id=batch_id,
                    call_index=call_index,
                    total_calls=total_calls,
                    tool_call=ToolCallView(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=dict(tool_call.arguments),
                    ),
                    tool_result=ToolResultView(
                        content=result.content,
                        is_error=result.is_error,
                        metadata=dict(result.metadata),
                    ),
                ),
            )
        return model_batch

    async def _emit_event(
        self,
        event_handler: RuntimeEventHandler | None,
        event: RuntimeEvent,
    ) -> None:
        if event_handler is None:
            return
        result = event_handler(event)
        if inspect.isawaitable(result):
            await result
