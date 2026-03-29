import unittest

from myopenclaw.agent.session_factory import SessionFactory


class SessionFactoryTests(unittest.TestCase):
    def test_new_session_binds_session_to_agent(self) -> None:
        session = SessionFactory(agent_id="Pickle").new_session("session-1")

        self.assertEqual("session-1", session.session_id)
        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual([], session.messages)


if __name__ == "__main__":
    unittest.main()
