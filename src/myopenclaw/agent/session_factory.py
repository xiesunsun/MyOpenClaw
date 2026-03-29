from dataclasses import dataclass
from uuid import uuid4

from myopenclaw.conversation.session import Session


@dataclass(frozen=True)
class SessionFactory:
    agent_id: str

    def new_session(self, session_id: str | None = None) -> Session:
        return Session(
            session_id=session_id or str(uuid4()),
            agent_id=self.agent_id,
        )
