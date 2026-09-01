"""从可靠事实、ModelCall 不可变内容与 Trace 轨迹导出诊断工作台报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Literal

from pickel.config.paths import home_dir
from pickel.conversations.agent_message import agent_message_to_dict
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
from pickel.model_calls.content_store import ModelCallContentStore
from pickel.observe.jsonl_trace_sink import trace_path
from pickel.observe.model_call_content_reader import ModelCallContentReader
from pickel.observe.operation_fact_reader import FactStore, OperationFactReader
from pickel.observe.operation_projector import OperationObservationProjector
from pickel.observe.operation_report_renderer import OperationReportRenderer
from pickel.observe.trace_reader import read_operation_trace


def export_operation_observation(
    operation_id: str,
    *,
    store: FactStore,
    content_store: ModelCallContentStore,
    trace_path_override: Path | None = None,
    out: Path | None = None,
    format: Literal["html", "json"] = "html",
) -> Path:
    """按 Unix 管道组合导出单个 Operation 的诊断数据或工作台 HTML。"""
    fact_reader = OperationFactReader(store)
    operation = fact_reader.read_operation(operation_id)
    if operation is None:
        raise ValueError(f"未找到 Operation: {operation_id}")

    content_reader = ModelCallContentReader(content_store)

    tp = trace_path_override or trace_path(operation.session_id)
    trace_data = read_operation_trace(tp, operation_id=operation_id)

    projector = OperationObservationProjector(
        fact_reader=fact_reader,
        content_reader=content_reader,
    )
    doc = projector.project_operation(operation_id, trace_data=trace_data)

    target = out or (home_dir() / "observations" / f"{operation_id}.{format}")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if format == "json":
        target.write_text(doc.to_json(), encoding="utf-8")
    else:
        renderer = OperationReportRenderer()
        target.write_text(renderer.render(doc), encoding="utf-8")

    return target


def export_operation_report(
    *,
    conversation_service: ConversationService,
    sessions: tuple[ConversationSession, ...],
    store: FactStore | None = None,
    content_store: ModelCallContentStore | None = None,
    out: Path | None = None,
) -> Path:
    """导出会话报告；有 Operation 时必须使用显式事实与内容依赖。"""
    if not sessions:
        raise ValueError("至少需要一个 ConversationSession")

    main_session = sessions[0]
    # 尝试找到该 Session 的活动或最近 Operation
    target_op_id = main_session.active_operation_id
    if target_op_id is None and store is not None:
        ops = store.list_operations(session_id=main_session.session_id)
        if ops:
            target_op_id = ops[-1].operation_id

    target = out or (home_dir() / "observations" / f"{main_session.session_id}.html")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target_op_id is not None:
        if store is None or content_store is None:
            raise ValueError("导出 Operation 报告需要显式提供 store 和 content_store")
        return export_operation_observation(
            target_op_id,
            store=store,
            content_store=content_store,
            out=target,
            format="html",
        )

    # 通用会话报告降级
    payload = []
    for session in sessions:
        nodes = conversation_service.list_active_branch_nodes(
            session_id=session.session_id
        )
        messages = []
        for node in nodes:
            if node.content_type != "agent_message":
                continue
            message = node.content
            messages.append(
                {
                    "node_id": node.node_id,
                    "parent_node_id": node.parent_node_id,
                    "created_at": node.created_at.isoformat(),
                    "message": agent_message_to_dict(message),  # type: ignore[arg-type]
                    "role": message.role,
                }
            )
        payload.append(
            {
                "session": {
                    "session_id": session.session_id,
                    "agent_id": session.agent_id,
                    "workspace_id": session.workspace_id,
                    "cwd": str(session.cwd),
                    "active_node_id": session.active_node_id,
                    "active_operation_id": session.active_operation_id,
                    "title": session.title,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "archived_at": (
                        session.archived_at.isoformat()
                        if session.archived_at is not None
                        else None
                    ),
                },
                "messages": messages,
                "events": _read_trace_events(trace_path(session.session_id)),
            }
        )
    target.write_text(_html_document(payload), encoding="utf-8")
    return target


def _read_trace_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _text(value: object) -> str:
    return html.escape("—" if value is None or value == "" else str(value))


def _json(value: object) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2))


def _message_content(message: dict) -> str:
    blocks = message.get("message", {}).get("content", [])
    rendered = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text":
            rendered.append(f'<p class="message-text">{_text(block.get("text"))}</p>')
        elif block_type == "thinking":
            rendered.append(
                f'<p class="muted message-text">思考：{_text(block.get("thinking"))}</p>'
            )
        else:
            rendered.append(f'<pre class="compact">{_json(block)}</pre>')
    return "".join(rendered) or '<p class="muted">无内容</p>'


def _event_view(event: dict) -> tuple[str, str, str, str]:
    record_type = str(event.get("record_type", "runtime_event"))
    if record_type == "span":
        payload = event.get("payload") or {}
        name = str(payload.get("name", "span"))
        status = str(payload.get("status", "unknown"))
        occurred_at = str(payload.get("started_at", event.get("recorded_at", "")))
        error = payload.get("error") or {}
        summary = str(error.get("message") or payload.get("attributes") or "")
        return name, status, occurred_at, summary
    name = str(event.get("event_type", record_type))
    status = "error" if name.endswith("failed") else "event"
    occurred_at = str(event.get("occurred_at", event.get("recorded_at", "")))
    summary = str(
        event.get("message")
        or event.get("text")
        or event.get("outcome")
        or event.get("user_text")
        or ""
    )
    return name, status, occurred_at, summary


def _session_section(item: dict) -> str:
    session = item["session"]
    messages = item["messages"]
    events = item["events"]
    failures = sum(_event_view(event)[1] == "error" for event in events)
    message_rows = (
        "".join(f"""<article class="message {message['role']}">
<header><span class="badge">{_text(message['role'])}</span>
<time>{_text(message['created_at'])}</time></header>
{_message_content(message)}
<footer>node {_text(message['node_id'])} · parent {_text(message['parent_node_id'])}</footer>
</article>""" for message in messages)
        or '<p class="empty">本次会话尚无已持久化消息</p>'
    )
    event_rows = (
        "".join(f"""<tr class="{_text(_event_view(event)[1])}">
<td><span class="status">{_text(_event_view(event)[1])}</span></td>
<td><strong>{_text(_event_view(event)[0])}</strong><br><span class="summary">{_text(_event_view(event)[3])}</span></td>
<td><time>{_text(_event_view(event)[2])}</time></td>
<td><code>{_text(event.get('operation_id'))}</code></td>
<td><details><summary>详情</summary><pre class="compact">{_json(event)}</pre></details></td>
</tr>""" for event in events)
        or '<tr><td colspan="5" class="empty">没有观测事件</td></tr>'
    )
    health = "异常" if failures else "正常"
    health_class = "danger" if failures else "ok"
    return f"""<section class="session">
<div class="session-head"><div><span class="eyebrow">CONVERSATION SESSION</span>
<h2>{_text(session.get('title') or session['agent_id'])}</h2>
<code>{_text(session['session_id'])}</code></div>
<span class="health {health_class}">{health}</span></div>
<div class="metrics">
<div><strong>{len(messages)}</strong><span>消息</span></div>
<div><strong>{len(events)}</strong><span>事件</span></div>
<div><strong>{failures}</strong><span>错误</span></div>
<div><strong>{_text(session.get('active_operation_id'))}</strong><span>活动 Operation</span></div>
</div>
<dl class="metadata">
<div><dt>Agent</dt><dd>{_text(session['agent_id'])}</dd></div>
<div><dt>Workspace</dt><dd>{_text(session['workspace_id'])}</dd></div>
<div><dt>工作目录</dt><dd>{_text(session['cwd'])}</dd></div>
<div><dt>更新时间</dt><dd>{_text(session['updated_at'])}</dd></div>
</dl>
<h3>对话</h3><div class="conversation">{message_rows}</div>
<h3>执行事件</h3><div class="table-wrap"><table>
<thead><tr><th>状态</th><th>事件</th><th>时间</th><th>Operation</th><th>原始记录</th></tr></thead>
<tbody>{event_rows}</tbody></table></div>
</section>"""


def _html_document(payload: list[dict]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    sections = "".join(_session_section(item) for item in payload)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pickel Operation Report</title>
<style>
:root{{--bg:#f4f7fb;--panel:#fff;--line:#dfe5ec;--text:#17212b;--muted:#667382;--blue:#2563eb;--red:#b42318;--red-bg:#fff1f0;--green:#067647;--green-bg:#ecfdf3}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1440px;margin:auto;padding:32px}} h1{{margin:4px 0;font-size:28px}} h2{{margin:3px 0 0;font-size:21px}} h3{{margin:28px 0 10px;font-size:15px}} code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}} .intro,.muted,time,footer,.summary{{color:var(--muted)}}
.session{{margin-top:24px;padding:24px;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 24px #1f29370a}} .session-head{{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}} .eyebrow{{font-size:11px;font-weight:700;letter-spacing:.12em;color:var(--blue)}}
.health,.status,.badge{{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:650}} .health.ok{{color:var(--green);background:var(--green-bg)}} .health.danger,.error .status{{color:var(--red);background:var(--red-bg)}} .event .status{{color:var(--blue);background:#eff6ff}} .ok .status{{color:var(--green);background:var(--green-bg)}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:20px 0}} .metrics div{{padding:12px 14px;background:#f8fafc;border:1px solid var(--line);border-radius:9px}} .metrics strong{{display:block;font-size:20px;overflow:hidden;text-overflow:ellipsis}} .metrics span{{color:var(--muted);font-size:12px}}
.metadata{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;margin:0;border:1px solid var(--line);border-radius:9px;overflow:hidden}} .metadata div{{display:grid;grid-template-columns:110px 1fr;padding:8px 11px;border-bottom:1px solid var(--line)}} .metadata div:nth-last-child(-n+2){{border-bottom:0}} dt{{color:var(--muted)}} dd{{margin:0;overflow-wrap:anywhere}}
.conversation{{display:grid;gap:10px}} .message{{border:1px solid var(--line);border-left:4px solid #94a3b8;border-radius:8px;padding:12px 14px}} .message.assistant{{border-left-color:var(--blue);background:#f8fbff}} .message header{{display:flex;justify-content:space-between;gap:12px}} .message footer{{font-size:11px;overflow-wrap:anywhere}} .message-text{{margin:10px 0;white-space:pre-wrap}} .badge{{background:#eef2f6}}
.table-wrap{{overflow:auto}} table{{width:100%;border-collapse:collapse}} th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:11px;text-transform:uppercase}} td:nth-child(2){{min-width:260px}} td:nth-child(3){{white-space:nowrap}} details summary{{cursor:pointer;color:var(--blue)}} pre.compact{{max-width:780px;margin:8px 0 0;padding:10px;background:#111827;color:#e5e7eb;border-radius:7px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}} .empty{{padding:22px;color:var(--muted);text-align:center}}
.raw{{margin-top:24px}} .raw>summary{{font-weight:650}} @media(max-width:760px){{main{{padding:16px}}.metrics,.metadata{{grid-template-columns:1fr}}.metadata div{{border-bottom:1px solid var(--line)!important}}}}
</style></head><body><main>
<span class="eyebrow">PICKEL OBSERVABILITY</span><h1>Operation Report</h1>
<p class="intro">Conversation 事实与运行轨迹。执行恢复以持久化 Operation State 为准，本报告仅用于诊断。</p>
{sections}
<details class="raw"><summary>完整原始数据</summary><pre data-report-json>{html.escape(encoded)}</pre></details>
</main></body></html>"""
