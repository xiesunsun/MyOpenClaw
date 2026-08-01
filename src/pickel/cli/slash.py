"""Slash 命令元数据、解析与 prompt_toolkit 补全。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    usage: str
    handler: str
    completion: str | None = None

    @property
    def token(self) -> str:
        return f"/{self.name}"


@dataclass(frozen=True)
class ParsedSlashCommand:
    name: str
    argument: str | None


class CompletionSources(Protocol):
    def complete(self, kind: str, argument: str) -> Iterable[str]: ...


class SlashRegistry:
    def __init__(self, commands: Iterable[SlashCommand]) -> None:
        self._commands = {item.name: item for item in commands}

    def list(self) -> tuple[SlashCommand, ...]:
        return tuple(self._commands[name] for name in sorted(self._commands))

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.removeprefix("/").lower())

    @property
    def command_line(self) -> str:
        return "  ".join(item.token for item in self.list())


def parse_slash(text: str) -> ParsedSlashCommand:
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    name = parts[0].removeprefix("/").lower()
    argument = parts[1].strip() if len(parts) > 1 else None
    return ParsedSlashCommand(name=name, argument=argument or None)


class SlashCompleter(Completer):
    """只补写输入文本，不执行业务。所有候选每次都向 Sources 现取。"""

    def __init__(
        self,
        registry: SlashRegistry,
        sources: CompletionSources,
    ) -> None:
        self._registry = registry
        self._sources = sources

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for command in self._registry.list():
                if command.token.startswith(text.lower()):
                    yield Completion(
                        command.token,
                        start_position=-len(text),
                        display_meta=command.summary,
                    )
            return

        token, argument = text.split(" ", 1)
        command = self._registry.get(token)
        if command is None or command.completion is None:
            return
        current = argument.rsplit(" ", 1)[-1]
        for value in self._sources.complete(command.completion, argument):
            if value.lower().startswith(current.lower()):
                yield Completion(value, start_position=-len(current))


BUILTIN_SLASH_COMMANDS = SlashRegistry(
    [
        SlashCommand("help", "Show this help message", "/help", "_command_help"),
        SlashCommand(
            "model",
            "List or set provider/model",
            "/model [provider/model]",
            "_command_model",
            "models",
        ),
        SlashCommand(
            "mcp",
            "Show MCP server status",
            "/mcp [server]",
            "_command_mcp",
            "mcp_servers",
        ),
        SlashCommand(
            "thinking",
            "Show or set thinking level",
            "/thinking [level]",
            "_command_thinking",
            "thinking",
        ),
        SlashCommand(
            "agent",
            "List agents or switch agent",
            "/agent [id]",
            "_command_agent",
            "agents",
        ),
        SlashCommand("new", "Start a new session", "/new", "_command_new"),
        SlashCommand(
            "reload", "Reload runtime resources", "/reload", "_command_reload"
        ),
        SlashCommand("context", "Show context usage", "/context", "_command_context"),
        SlashCommand("session", "Show session details", "/session", "_command_session"),
        SlashCommand(
            "skills",
            "Review skill writes",
            "/skills [pending|diff|approve|reject] [id]",
            "_command_skills",
            "skills",
        ),
        SlashCommand(
            "tools",
            "List active tools",
            "/tools [name]",
            "_command_tools",
            "tools",
        ),
        SlashCommand("clear", "Clear the screen", "/clear", "_command_clear"),
        SlashCommand("exit", "Exit the chat loop", "/exit", "_command_exit"),
    ]
)
