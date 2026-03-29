from dataclasses import dataclass
import inspect
import time

from myopenclaw.agent.agent import Agent
from myopenclaw.conversation.message import ToolCall
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.session import Session
from myopenclaw.llm import BaseLLMProvider, create_llm_provider
from myopenclaw.runtime.events import RuntimeEvent, RuntimeEventHandler, RuntimeEventType
from myopenclaw.runtime.generation import FinishReason, GenerateRequest, GenerateResult
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.registry import ToolRegistry


class DefaultProviderResolver:
    def resolve(self, agent: Agent) -> BaseLLMProvider:
        return create_llm_provider(agent.model_config)


class DefaultToolResolver:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry(tools=builtin_tools())

    def resolve(self, agent: Agent) -> list[BaseTool]:
        return self.registry.resolve_many(agent.tool_ids)


@dataclass
class TurnRunner:
    provider_resolver: DefaultProviderResolver | object = DefaultProviderResolver()
    tool_resolver: DefaultToolResolver | object = DefaultToolResolver()
    max_steps: int = 8

    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        if session.agent_id != agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{agent.agent_id}'"
            )

        provider = self.provider_resolver.resolve(agent)
        tools = self.tool_resolver.resolve(agent)
        session.append_user_message(user_text)
        return await self._run_react_loop(
            agent=agent,
            provider=provider,
            tools=tools,
            session=session,
            event_handler=event_handler,
        )

    async def _run_react_loop(
        self,
        *,
        agent: Agent,
        provider: BaseLLMProvider,
        tools: list[BaseTool],
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
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
            result = await provider.generate(
                GenerateRequest(
                    system_instruction=agent.system_instruction or None,
                    messages=list(session.messages),
                    tools=[tool.spec for tool in tools],
                )
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            metadata = result.metadata or MessageMetadata(
                provider=agent.model_config.provider,
                model=agent.model_config.model,
                input_tokens=result.usage.input_tokens if result.usage else None,
                output_tokens=result.usage.output_tokens if result.usage else None,
                elapsed_ms=elapsed_ms,
            )
            result.metadata = metadata
            last_metadata = metadata

            if result.tool_calls:
                session.append_assistant_message(
                    content=result.text,
                    metadata=metadata,
                    tool_calls=result.tool_calls,
                )
                for tool_call in result.tool_calls:
                    await self._emit_event(
                        event_handler,
                        RuntimeEvent(
                            event_type=RuntimeEventType.TOOL_CALL_STARTED,
                            step_index=step_index,
                            tool_call=tool_call,
                        ),
                    )
                    tool_result = await self._execute_tool_call(
                        agent=agent,
                        tools=tools,
                        session=session,
                        tool_call=tool_call,
                    )
                    session.append_tool_result(
                        content=tool_result.content,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        is_error=tool_result.is_error,
                    )
                    await self._emit_event(
                        event_handler,
                        RuntimeEvent(
                            event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                            step_index=step_index,
                            tool_call=tool_call,
                            tool_result=tool_result,
                        ),
                    )
                continue

            session.append_assistant_message(result.text, metadata=metadata)
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    step_index=step_index,
                    text=result.text,
                    metadata=metadata,
                ),
            )
            return result

        result = GenerateResult(
            text="Reached the maximum number of reasoning steps.",
            finish_reason=FinishReason.MAX_STEPS,
            metadata=last_metadata,
        )
        session.append_assistant_message(result.text, metadata=last_metadata)
        await self._emit_event(
            event_handler,
            RuntimeEvent(
                event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                step_index=self.max_steps,
                text=result.text,
                metadata=last_metadata,
            ),
        )
        return result

    async def _execute_tool_call(
        self,
        *,
        agent: Agent,
        tools: list[BaseTool],
        session: Session,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        tool = next((candidate for candidate in tools if candidate.spec.name == tool_call.name), None)
        if tool is None:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
            )

        context = ToolExecutionContext(
            agent_id=agent.agent_id,
            session_id=session.session_id,
            workspace_path=agent.workspace,
        )
        try:
            return await tool.execute(tool_call.arguments, context)
        except Exception as exc:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' failed: {exc}",
                is_error=True,
            )

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
