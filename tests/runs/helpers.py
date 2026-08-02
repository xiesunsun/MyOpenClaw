from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent


def user_message(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])
