import unittest

from myopenclaw.conversations.session import Session
from myopenclaw.integrations.openviking.config import (
    OpenVikingConfig,
    OpenVikingSessionRecallConfig,
)
from myopenclaw.integrations.openviking.session_recall import (
    OpenVikingSessionRecallProvider,
)


class FakeOpenVikingResult:
    def __init__(
        self,
        resources: list[object] | None = None,
        memories: list[object] | None = None,
    ) -> None:
        self.resources = resources or []
        self.memories = memories or []


class FakeOpenVikingItem:
    def __init__(
        self,
        *,
        uri: str,
        score: float = 0.5,
        abstract: str | None = None,
        overview: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.uri = uri
        self.score = score
        self.abstract = abstract
        self.overview = overview
        self.kind = kind


class FakeOpenVikingClient:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []
        self.find_calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> FakeOpenVikingResult:
        self.search_calls.append(kwargs)
        return FakeOpenVikingResult(
            memories=[
                FakeOpenVikingItem(
                    uri="viking://session/u/session-remote/.abstract.md",
                    score=0.99,
                    abstract="Generated session abstract should be filtered.",
                ),
                FakeOpenVikingItem(
                    uri="viking://session/u/session-remote/message-1.md",
                    score=0.9,
                    abstract="User: 你好\nAssistant: 你好呀",
                ),
                FakeOpenVikingItem(
                    uri="viking://session/u/session-remote/nested",
                    score=0.8,
                    abstract="Directory should be filtered.",
                    kind="directory",
                ),
            ]
        )

    def find(self, **kwargs: object) -> object:
        self.find_calls.append(kwargs)
        raise AssertionError("session recall should not call find")


class FailingOpenVikingClient:
    def search(self, **kwargs: object) -> object:
        raise RuntimeError("search failed")


class OpenVikingSessionRecallProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_recalls_session_context_with_search_only(self) -> None:
        client = FakeOpenVikingClient()
        provider = OpenVikingSessionRecallProvider(
            config=_config(),
            client=client,
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.remote_session_id = "session-remote"

        result = await provider.recall(
            session=session,
            current_user_text="之前说了什么",
        )

        self.assertEqual(1, len(client.search_calls))
        self.assertEqual("session-remote", client.search_calls[0]["session_id"])
        self.assertEqual([], client.find_calls)
        self.assertEqual(1, len(result.snippets))
        self.assertIn("User: 你好", result.snippets[0].text)
        self.assertNotIn("Generated session abstract", result.snippets[0].text)

    async def test_skips_without_remote_session_id(self) -> None:
        client = FakeOpenVikingClient()
        provider = OpenVikingSessionRecallProvider(
            config=_config(),
            client=client,
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await provider.recall(session=session, current_user_text="hello")

        self.assertTrue(result.is_empty)
        self.assertEqual([], client.search_calls)

    async def test_failure_returns_empty_result(self) -> None:
        provider = OpenVikingSessionRecallProvider(
            config=_config(),
            client=FailingOpenVikingClient(),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.remote_session_id = "session-remote"

        result = await provider.recall(session=session, current_user_text="hello")

        self.assertTrue(result.is_empty)


def _config() -> OpenVikingConfig:
    return OpenVikingConfig(
        enabled=True,
        base_url="https://openviking.example",
        account_id="account",
        user_id="u",
        user_key="secret",
        session_recall=OpenVikingSessionRecallConfig(
            enabled=True,
            max_chars=6000,
            limit=3,
        ),
    )


if __name__ == "__main__":
    unittest.main()
