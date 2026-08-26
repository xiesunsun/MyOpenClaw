import asyncio
import unittest
from unittest.mock import MagicMock

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pickel.cli.chat import ChatLoop
from pickel.cli.slash import BUILTIN_SLASH_COMMANDS, SlashCompleter, parse_slash


class _Sources:
    def __init__(self) -> None:
        self.pending = ["AbC123"]
        self.mcp_servers = ["GitHub"]

    def complete(self, kind: str, argument: str):
        if kind == "skills":
            if argument.startswith("approve "):
                return tuple(self.pending)
            return ("pending", "diff", "approve", "reject")
        if kind == "mcp_servers":
            return tuple(self.mcp_servers)
        return ()


def _values(completer: SlashCompleter, text: str) -> list[str]:
    return [
        item.text
        for item in completer.get_completions(
            Document(text=text),
            CompleteEvent(completion_requested=True),
        )
    ]


class SlashCompleterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = _Sources()
        self.completer = SlashCompleter(BUILTIN_SLASH_COMMANDS, self.sources)

    def test_only_slash_input_has_completions(self) -> None:
        self.assertEqual([], _values(self.completer, "hello"))
        self.assertEqual([], _values(self.completer, "/mo"))
        self.assertIn("/mcp", _values(self.completer, "/mc"))

    def test_model_and_thinking_are_not_registered_or_completed(self) -> None:
        self.assertIsNone(BUILTIN_SLASH_COMMANDS.get("model"))
        self.assertIsNone(BUILTIN_SLASH_COMMANDS.get("thinking"))
        self.assertEqual([], _values(self.completer, "/model "))
        self.assertEqual([], _values(self.completer, "/thinking "))

    def test_removed_commands_are_not_dispatched(self) -> None:
        loop = object.__new__(ChatLoop)
        loop._slash_registry = BUILTIN_SLASH_COMMANDS
        loop._render_error_message = MagicMock()

        asyncio.run(loop._handle_command("/model anthropic/test"))
        asyncio.run(loop._handle_command("/thinking high"))

        self.assertEqual(2, loop._render_error_message.call_count)
        self.assertIn(
            "Unknown command", loop._render_error_message.call_args_list[0].args[0]
        )

    def test_nested_skill_completion_preserves_candidate_case(self) -> None:
        self.assertEqual(
            ["AbC123"],
            _values(self.completer, "/skills approve A"),
        )

    def test_mcp_server_completion_reads_current_status_source(self) -> None:
        self.assertEqual(["GitHub"], _values(self.completer, "/mcp G"))
        self.sources.mcp_servers = ["GitLab"]
        self.assertEqual(["GitLab"], _values(self.completer, "/mcp G"))

    def test_parser_only_normalizes_command_name(self) -> None:
        parsed = parse_slash("/MODEL anthropic/ClaudeCase")
        self.assertEqual("model", parsed.name)
        self.assertEqual("anthropic/ClaudeCase", parsed.argument)
