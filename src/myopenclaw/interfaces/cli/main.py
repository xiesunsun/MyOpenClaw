import asyncio
from pathlib import Path

import typer

from myopenclaw.interfaces.cli.chat import ChatLoop

app = typer.Typer()


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    asyncio.run(ChatLoop.from_config_path(config_path=config, agent_id=agent).run())


if __name__ == "__main__":
    app()
