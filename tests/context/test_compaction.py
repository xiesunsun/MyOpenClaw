from pickel.context.compaction import apply_compaction, plan_keep_last_units
from pickel.context.projection import project_messages
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session


def test_plan_and_apply_compaction_keeps_tail_units():
    session = Session.create(agent_id="Pickle")
    for i in range(4):
        session.append_user(UserMessage(content=[TextContent(text=f"u{i}")]))
        session.append_assistant(AssistantMessage(content=[TextContent(text=f"a{i}")]))
    plan = plan_keep_last_units(session, keep_units=4, summary="earlier dropped")
    assert plan is not None
    apply_compaction(session, plan)
    messages = project_messages(session.active_path())
    assert messages[0].content[0].text.startswith("[compaction]")
    # 2 units: (u2,a2) (u3,a3) after summary
    texts = []
    for m in messages[1:]:
        if m.content and hasattr(m.content[0], "text"):
            texts.append(m.content[0].text)
    assert "u2" in texts  # last two turns
    assert "u3" in texts
    assert "u0" not in texts
