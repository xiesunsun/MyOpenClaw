import asyncio
import sqlite3
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pickel.app.boot import Boot
from pickel.app.application import RuntimeApplication
from pickel.cli.chat import ChatLoop
from pickel.cli.query import QuerySurface
from pickel.cli.query_input import read_query_input
from pickel.app.runtime_models import (
    ConversationRequest,
    RuntimeLaunchRequest,
    AgentRunRequest,
)
from pickel.config.loader import Config
from pickel.config.paths import runtime_db_path
from pickel.conversations.conversation_service import (
    ConversationNotFoundError,
    ConversationService,
)
from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.persistence.sqlite_runtime_store import (
    SQLiteRuntimeStore,
    UnsupportedStorageSchemaError,
)
from pickel.persistence.sqlite_schema_v12 import UnsupportedSchemaVersionError
from pickel.persistence.errors import StorageIntegrityError

app = typer.Typer(invoke_without_command=True)
sessions_app = typer.Typer(invoke_without_command=True)
config_app = typer.Typer(help="配置相关命令")
QUERY_DEFAULT_AGENT = "shell"


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


def _run_chat(
    *,
    agent: str | None,
    session_id: str | None,
) -> None:
    async def _main() -> None:
        async with RuntimeApplication.open(
            RuntimeLaunchRequest(cwd=Path.cwd())
        ) as runtime:
            for warning in runtime.warnings:
                typer.secho(
                    f"Extension load error: {warning}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
            assert runtime.host is not None
            await ChatLoop.from_host(
                host=runtime.host,
                agent_id=agent,
                session_id=session_id,
            ).run()

    try:
        asyncio.run(_main())
    except ConversationNotFoundError as exc:
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


def _run_query(
    *,
    query: str,
    agent: str | None,
    session_id: str | None,
    save_session: bool,
    output_format: str,
) -> None:
    if output_format not in {"text", "json", "jsonl"}:
        typer.echo("--output-format 须为 text、json 或 jsonl", err=True)
        raise typer.Exit(code=2)

    try:
        user_message = read_query_input(query, sys.stdin).to_user_message()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    async def _main():
        resolved_agent = agent
        if resolved_agent is None and session_id is None:
            resolved_agent = QUERY_DEFAULT_AGENT
        launch_agent_ids = (resolved_agent,) if resolved_agent is not None else None
        async with RuntimeApplication.open(
            RuntimeLaunchRequest(
                cwd=Path.cwd(),
                agent_ids=launch_agent_ids,
                session_id=(session_id if resolved_agent is None else None),
            )
        ) as runtime:
            for warning in runtime.warnings:
                typer.secho(
                    f"Extension load error: {warning}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
            assert runtime.host is not None
            conversation = runtime.host.open_conversation(
                ConversationRequest(
                    agent_id=resolved_agent,
                    session_id=session_id,
                    persistence=(
                        "persistent"
                        if save_session or session_id is not None
                        else "ephemeral"
                    ),
                    cwd=Path.cwd(),
                )
            )
            return await QuerySurface(
                stdout=sys.stdout,
                output_format=output_format,  # type: ignore[arg-type]
            ).run(
                conversation=conversation,
                request=AgentRunRequest(message=user_message),
            )

    try:
        result = asyncio.run(_main())
    except ConversationNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc).strip("'"), err=True)
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt as exc:
        raise typer.Exit(code=130) from exc

    if result.status == "blocked":
        raise typer.Exit(code=3)
    if result.status == "cancelled":
        raise typer.Exit(code=130)
    if result.status == "failed":
        if result.error is not None:
            typer.echo(
                f"{result.error.error_type}: {result.error.message}",
                err=True,
            )
        raise typer.Exit(code=1)


@app.callback()
def main(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent"),
    session_id: str | None = typer.Option(None, "--session-id"),
    query: str | None = typer.Option(None, "--query", "-q"),
    output_format: str = typer.Option("text", "--output-format"),
    save_session: bool = typer.Option(False, "--save-session"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if query is not None:
        _run_query(
            query=query,
            agent=agent,
            session_id=session_id,
            save_session=save_session,
            output_format=output_format,
        )
        return
    if output_format != "text" or save_session:
        typer.echo(
            "--output-format 与 --save-session 只能和 -q/--query 一起使用",
            err=True,
        )
        raise typer.Exit(code=2)
    _run_chat(agent=agent, session_id=session_id)


@app.command()
def chat(
    agent: str | None = typer.Option(None, "--agent"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    _run_chat(agent=agent, session_id=session_id)


observe_app = typer.Typer(
    invoke_without_command=True, help="可观测系统与故障诊断数据工作台"
)


@observe_app.callback()
def observe(
    ctx: typer.Context = None,  # type: ignore[assignment]
    session: list[str] = typer.Option([], "--session", help="指定 session_id，可多次"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="输出 HTML 路径；默认文件名包含主 session_id",
    ),
    limit: int = typer.Option(
        20, "--limit", help="无 --session 时导出最近 N 个含消息的会话"
    ),
) -> None:
    """导出会话执行轨迹为自包含 HTML 观测平台。"""
    if ctx is not None and ctx.invoked_subcommand is not None:
        return

    from pickel.observe.operation_report import export_operation_report

    try:
        # 观测是只读查询，不应装载 Config、Provider、MCP 或 Extension。
        store = SQLiteRuntimeStore(runtime_db_path())
        service = ConversationService(store)
        if session:
            loaded_sessions = []
            for session_id in session:
                try:
                    loaded_sessions.append(
                        service.load_conversation_session(session_id)
                    )
                except ConversationNotFoundError:
                    continue
        else:
            loaded_sessions = [
                service.load_conversation_session(preview.session_id)
                for preview in service.list_conversation_previews(
                    limit=limit,
                    all_sessions=True,
                )
                if preview.message_count > 0
            ]
        if not loaded_sessions:
            typer.echo("没有可导出的会话", err=True)
            raise typer.Exit(code=1)

        typer.echo(
            str(
                export_operation_report(
                    conversation_service=service,
                    sessions=tuple(loaded_sessions),
                    store=store,
                    content_store=store.model_call_content_store,
                    out=out,
                )
            )
        )
    except (
        UnsupportedStorageSchemaError,
        UnsupportedSchemaVersionError,
        StorageIntegrityError,
        sqlite3.DatabaseError,
        ValueError,
        OSError,
    ) as exc:
        typer.echo(f"无法读取观测数据：{exc}", err=True)
        raise typer.Exit(code=1) from exc


@observe_app.command("operation")
def observe_operation(
    operation_id: str = typer.Argument(..., help="要诊断的 Operation ID"),
    format: str = typer.Option("html", "--format", "-f", help="输出格式：html 或 json"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        "--out",
        help="输出文件路径；默认保存至 ~/.pickel/observations/<op_id>.<format>",
    ),
) -> None:
    """导出单个 Operation 的诊断数据工作台 (HTML 或 JSON)。"""
    if format not in ("html", "json"):
        typer.echo("--format 必须是 html 或 json", err=True)
        raise typer.Exit(code=2)

    from pickel.observe.operation_report import export_operation_observation

    # 观测是只读查询，不应装载 Config、Provider、MCP 或 Extension。
    store = SQLiteRuntimeStore(runtime_db_path())
    try:
        exported_path = export_operation_observation(
            operation_id=operation_id,
            store=store,
            content_store=store.model_call_content_store,
            out=output,
            format=format,  # type: ignore[arg-type]
        )
    except (
        UnsupportedStorageSchemaError,
        UnsupportedSchemaVersionError,
        StorageIntegrityError,
        sqlite3.DatabaseError,
        ValueError,
        OSError,
    ) as exc:
        typer.echo(f"无法读取观测数据：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(str(exported_path))


@observe_app.command("serve")
def observe_serve(
    session: str = typer.Option(..., "--session", help="要查看的 Session ID"),
    port: int = typer.Option(8765, "--port", min=0, max=65535, help="本地监听端口"),
) -> None:
    """在 127.0.0.1 提供只读观测 API 与工作台数据。"""

    # 这里故意不调用 _boot()：观测服务只能打开 SQLiteRuntimeStore 与
    # ModelCallContentStore，不能装载 Config、Provider、MCP 或 Extension。
    from pickel.observe.http_server import serve_observation

    try:
        store = SQLiteRuntimeStore(runtime_db_path())
        serve_observation(
            store=store,
            content_store=store.model_call_content_store,
            session_id=session,
            port=port,
        )
    except (
        UnsupportedStorageSchemaError,
        UnsupportedSchemaVersionError,
        StorageIntegrityError,
        sqlite3.DatabaseError,
        ValueError,
        OSError,
    ) as exc:
        typer.echo(f"无法启动观测服务：{exc}", err=True)
        raise typer.Exit(code=1) from exc


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
    service = boot.build_conversation_service(store=boot.runtime_store())
    previews = service.list_conversation_previews(all_sessions=all_sessions)
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
    service = boot.build_conversation_service(store=boot.runtime_store())
    try:
        service.delete_conversation_session(session_id=session_id)
    except ConversationNotFoundError as exc:
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
app.add_typer(observe_app, name="observe")


if __name__ == "__main__":
    app()
