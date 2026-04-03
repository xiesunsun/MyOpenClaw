from pathlib import Path

import typer

from myopenclaw.bootstrap.assembly import BootstrapAssembly
from myopenclaw.interfaces.cli.chat import ChatLoop


app = typer.Typer()


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    assembly = BootstrapAssembly.from_config_path(config)
    loop = ChatLoop(
        chat_service=assembly.build_chat_service(agent_id=agent),
        config_path=config,
    )
    import asyncio

    asyncio.run(loop.run())


if __name__ == "__main__":
    app()
