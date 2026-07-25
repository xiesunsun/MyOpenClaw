import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from myopenclaw.app.boot import Boot
from myopenclaw.cli.chat import ChatLoop
from myopenclaw.config.loader import Config
from myopenclaw.conversations.service import SessionNotFoundError

app = typer.Typer(invoke_without_command=True)
sessions_app = typer.Typer(invoke_without_command=True)
config_app = typer.Typer(help="配置相关命令")


def _resolve_boot(config: Path | None) -> Boot:
    """无 --config 时用分层 Config.load；有路径时走旧 yaml。"""
    if config is None:
        return Boot.from_config(Config.load(cwd=Path.cwd()))
    return Boot.from_config_path(config)


def _run_chat(
    *,
    config: Path | None,
    agent: str | None,
    session_id: str | None,
) -> None:
    try:
        asyncio.run(
            ChatLoop.from_boot(
                boot=_resolve_boot(config),
                agent_id=agent,
                session_id=session_id,
                config_path=config,
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
    config: Path | None = typer.Option(None, "--config"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _run_chat(config=config, agent=agent, session_id=session_id)


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    config: Path | None = typer.Option(None, "--config"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    _run_chat(config=config, agent=agent, session_id=session_id)


@sessions_app.callback()
def sessions(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config"),
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="列出全部会话（不按当前目录过滤）",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    # 默认只显示当前 cwd 下的会话
    previews = (
        _resolve_boot(config)
        .build_session_service()
        .list_sessions(all_sessions=all_sessions)
    )
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


@sessions_app.command("delete")
def delete_session(
    session_id: str = typer.Argument(...),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    boot = _resolve_boot(config)
    lookup_service = boot.build_session_service()
    try:
        session = lookup_service.resume(session_id=session_id)
        delete_service = boot.build_session_service(agent_id=session.agent_id)
        delete_service.delete(session_id=session_id)
    except SessionNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Deleted session {session_id}")


@config_app.command("migrate")
def config_migrate(
    from_path: Path = typer.Option(
        ...,
        "--from",
        help="旧 config.yaml 路径",
    ),
) -> None:
    """将旧 config.yaml 迁移为分层 settings/models/auth 与 agents。"""
    from myopenclaw.config.migrate import migrate_from_yaml

    try:
        summary = migrate_from_yaml(from_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"home: {summary['home']}")
    typer.echo(f"settings: {summary['settings']}")
    typer.echo(f"models: {summary['models']}")
    typer.echo(f"auth: {summary['auth']}")
    for agent in summary.get("agents") or []:
        typer.echo(f"agent: {agent['id']} -> {agent['path']}")
    sessions = summary.get("sessions") or {}
    typer.echo(f"sessions: {sessions.get('action')}")
    if summary.get("config_backup"):
        typer.echo(f"config_backup: {summary['config_backup']}")
    for warning in summary.get("warnings") or []:
        typer.echo(f"warning: {warning}", err=True)


@config_app.command("set-default-model")
def config_set_default_model(
    provider: str = typer.Argument(..., help="provider 名，如 anthropic"),
    model: str = typer.Argument(..., help="model 名"),
    scope: str = typer.Option(
        "global",
        "--scope",
        help="写入范围：global（~/.pickel）或 project（项目 .pickel）",
    ),
) -> None:
    """持久化 default_llm 到 settings.json（会话内临时切换用 Environ，与此独立）。"""
    from myopenclaw.config.settings import set_default_llm
    from myopenclaw.shared.model_config import ModelSelection

    if scope not in ("global", "project"):
        typer.echo("scope 须为 global 或 project", err=True)
        raise typer.Exit(code=1)

    try:
        path = set_default_llm(
            ModelSelection(provider=provider, model=model),
            scope=scope,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"default_llm -> {provider}/{model}")
    typer.echo(f"settings: {path}")


app.add_typer(sessions_app, name="sessions")
app.add_typer(config_app, name="config")


if __name__ == "__main__":
    app()
