import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pickel.app.boot import Boot
from pickel.cli.chat import ChatLoop
from pickel.config.loader import Config
from pickel.conversations.service import SessionNotFoundError
from pickel.extensions_host.loader import load_extensions, load_extensions_async
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools

app = typer.Typer(invoke_without_command=True)
sessions_app = typer.Typer(invoke_without_command=True)
config_app = typer.Typer(help="配置相关命令")


def _prepare_boot() -> tuple[Config, ToolBus]:
    app_config = Config.load(cwd=Path.cwd())
    tool_bus = ToolBus()
    install_builtin_tools(tool_bus)
    return app_config, tool_bus


def _finish_boot(app_config: Config, tool_bus: ToolBus, result) -> Boot:
    # 装载错误只警告、不阻止启动：一个坏 extension 不该弄挂 CLI
    for error in result.errors:
        typer.secho(f"Extension load error: {error}", fg=typer.colors.YELLOW, err=True)
    boot = Boot.from_config(app_config, tool_bus=tool_bus, extensions=result.registry)
    boot.extension_result = result
    return boot


def _boot() -> Boot:
    """同步启动路径（chat 之外的命令）：分层 Config.load + extension 装载。"""
    app_config, tool_bus = _prepare_boot()
    result = load_extensions(tool_bus=tool_bus, app_config=app_config)
    return _finish_boot(app_config, tool_bus, result)


async def _boot_async() -> Boot:
    """chat 的启动路径：extension 装载发生在 chat 自己的事件循环里。

    MCP 连接由背景任务持有——若在临时 asyncio.run 里装载，
    进入 chat 循环时连接已随旧循环死掉，首次调用只能靠重连兜住。
    """
    app_config, tool_bus = _prepare_boot()
    result = await load_extensions_async(tool_bus=tool_bus, app_config=app_config)
    return _finish_boot(app_config, tool_bus, result)


def _run_chat(
    *,
    agent: str | None,
    session_id: str | None,
) -> None:
    async def _main() -> None:
        await ChatLoop.from_boot(
            boot=await _boot_async(),
            agent_id=agent,
            session_id=session_id,
        ).run()

    try:
        asyncio.run(_main())
    except SessionNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (KeyError, ValueError) as exc:
        message = str(exc).strip("'")
        if session_id is not None and message.startswith("Unknown agent: "):
            typer.echo(
                f"Unable to resume session {session_id}: agent '{message.removeprefix('Unknown agent: ')}' is no longer configured.",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        if isinstance(exc, ValueError):
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        raise


@app.callback()
def main(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _run_chat(agent=agent, session_id=session_id)


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    _run_chat(agent=agent, session_id=session_id)


@app.command()
def observe(
    session: list[str] = typer.Option([], "--session", help="指定 session_id，可多次"),
    out: Path = typer.Option(Path("pickel-observe.html"), "--out", help="输出 HTML 路径"),
    limit: int = typer.Option(20, "--limit", help="无 --session 时导出最近 N 个含消息的会话"),
) -> None:
    """导出会话执行轨迹为自包含 HTML 观测平台。"""
    from datetime import datetime, timezone

    from pickel.config.paths import sessions_db_path
    from pickel.observe.collector import collect_previews, collect_trajectory
    from pickel.observe.html_report import render_html
    from pickel.observe.trace_reader import read_trace
    from pickel.persistence.sqlite_session_repository import (
        SQLiteSessionRepository,
    )
    from pickel.runs.trace_sink import trace_path

    repository = SQLiteSessionRepository(sessions_db_path())
    if session:
        sessions = [
            loaded for sid in session if (loaded := repository.load(sid))
        ]
    else:
        sessions = collect_previews(repository, limit=limit)
    if not sessions:
        typer.echo("没有可导出的会话", err=True)
        raise typer.Exit(code=1)

    trajectories = [
        collect_trajectory(
            item, enhancement=read_trace(trace_path(item.session_id))
        )
        for item in sessions
    ]
    out.write_text(
        render_html(
            trajectories,
            generated_at=datetime.now(timezone.utc).isoformat(),
        ),
        encoding="utf-8",
    )
    typer.echo(str(out.resolve()))


@sessions_app.callback()
def sessions(
    ctx: typer.Context,
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="列出全部会话（不按当前目录过滤）",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    try:
        boot = _boot()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    previews = boot.build_session_service().list_sessions(all_sessions=all_sessions)
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
) -> None:
    try:
        boot = _boot()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
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
        help="旧 config.yaml 路径（仅迁移用，运行时不再读取）",
    ),
) -> None:
    """将旧 config.yaml 一次性迁移为 ~/.pickel 分层文件与 agents/*.yaml。"""
    from pickel.config.migrate import migrate_from_yaml

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
    """持久化 default_llm 到 settings.json。"""
    from pickel.config.settings import set_default_llm
    from pickel.shared.model_config import ModelSelection

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
