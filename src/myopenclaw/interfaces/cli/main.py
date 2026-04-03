import asyncio
from pathlib import Path

import typer

from myopenclaw.interfaces.cli.chat import ChatLoop


def create_app(chat_loop_factory) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def chat(
        agent: str | None = typer.Option(None, "--agent"),
        config: Path = typer.Option(Path("config.yaml"), "--config"),
    ) -> None:
        asyncio.run(chat_loop_factory(config_path=config, agent_id=agent).run())

    return app


__all__ = ["ChatLoop", "create_app"]
