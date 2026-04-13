import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from myopenclaw.app.assembly import AppAssembly
from myopenclaw.cli.chat import ChatLoop
from myopenclaw.conversations.service import SessionNotFoundError

app = typer.Typer(invoke_without_command=True)


def _run_chat(
    *,
    config: Path,
    agent: str | None,
    session_id: str | None,
) -> None:
    try:
        asyncio.run(
            ChatLoop.from_config_path(
                config_path=config,
                agent_id=agent,
                session_id=session_id,
            ).run()
        )
    except SessionNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except KeyError as exc:
        message = str(exc).strip("'")
        if session_id is not None and message.startswith("Unknown agent: "):
            typer.echo(
                f"Unable to resume session {session_id}: agent '{message.removeprefix('Unknown agent: ')}' is no longer configured.",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        raise


@app.callback()
def main(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _run_chat(config=config, agent=agent, session_id=session_id)


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    _run_chat(config=config, agent=agent, session_id=session_id)


@app.command("sessions")
def list_sessions(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    previews = AppAssembly.from_config_path(config).build_session_service().list_sessions()
    table = Table(title="Sessions")
    table.add_column("session id", overflow="ignore", no_wrap=True)
    table.add_column("agent id")
    table.add_column("status")
    table.add_column("message count", justify="right")
    table.add_column("updated at")
    table.add_column("last message")
    for preview in previews:
        table.add_row(
            preview.session_id,
            preview.agent_id,
            preview.status,
            str(preview.message_count),
            preview.updated_at.isoformat(),
            preview.last_message,
        )
    Console().print(table)


if __name__ == "__main__":
    app()
