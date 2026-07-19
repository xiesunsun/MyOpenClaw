from myopenclaw.context.model_context import ModelContext, SystemContent
from myopenclaw.context.observation import ContextObservation
from myopenclaw.conversations.agent_message import UserMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.cli.context_renderer import ModelContextRenderer


def test_observation_predicted_flag():
    obs = ContextObservation(
        model_context=ModelContext(
            system=SystemContent.from_text("sys"),
            messages=[UserMessage(content=[TextContent(text="hi")])],
        ),
        predicted=True,
    )
    renderable = ModelContextRenderer().render_observation(obs)
    assert renderable is not None
