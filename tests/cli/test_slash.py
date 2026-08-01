import unittest

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pickel.cli.slash import BUILTIN_SLASH_COMMANDS, SlashCompleter, parse_slash


class _Sources:
    def __init__(self) -> None:
        self.models = ["anthropic/ClaudeCase"]
        self.pending = ["AbC123"]
        self.mcp_servers = ["GitHub"]

    def complete(self, kind: str, argument: str):
        if kind == "models":
            return tuple(self.models)
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
        self.assertIn("/model", _values(self.completer, "/mo"))

    def test_argument_completion_reads_current_source_each_time(self) -> None:
        self.assertEqual(
            ["anthropic/ClaudeCase"],
            _values(self.completer, "/model anth"),
        )
        self.sources.models = ["anthropic/Changed"]
        self.assertEqual(
            ["anthropic/Changed"],
            _values(self.completer, "/model anth"),
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
