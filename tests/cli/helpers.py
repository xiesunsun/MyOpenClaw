from __future__ import annotations

from collections.abc import Awaitable, Callable

import pickel.cli.chat as chat_module
from pickel.agents.agent import Agent
from pickel.app.boot import Boot
from pickel.app.runtime import RuntimeConversation, RuntimeHost
from pickel.cli.chat import ChatLoop
from pickel.cli.context_renderer import ContextRenderer
from pickel.config.app_config import AppConfig
from pickel.conversations.service import SessionService
from pickel.conversations.session import Session
from pickel.runs.run import Run
from pickel.runs.trace_sink import JsonlTraceSink
from rich.console import Console


def chat_loop(
    *,
    agent: Agent,
    agent_id: str | None = None,
    run: Run | None = None,
    session: Session | None = None,
    console: Console | None = None,
    input_reader: Callable[[str], str | Awaitable[str]] | None = None,
    context_renderer: ContextRenderer | None = None,
    session_service: SessionService | None = None,
    boot: Boot | None = None,
    app_config: AppConfig | None = None,
) -> ChatLoop:
    resolved_agent_id = agent_id or agent.agent_id
    conversation = RuntimeConversation(
        agent=agent,
        run=run,
        session=session or Session.create(agent_id=resolved_agent_id),
        session_service=session_service,
        app_config=app_config or (boot.app_config if boot is not None else None),
        trace_path_resolver=chat_module.trace_path,
        trace_sink_factory=JsonlTraceSink,
    )
    return ChatLoop(
        host=RuntimeHost(boot) if boot is not None else None,
        conversation=conversation,
        console=console,
        input_reader=input_reader,
        context_renderer=context_renderer,
    )
