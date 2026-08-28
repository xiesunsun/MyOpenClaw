"""Pickel 观测数据的本地只读 HTTP 传输层。

该模块刻意只依赖标准库。它接受已经构造好的 Store，不调用 Boot、Runtime、
Provider、MCP 或 Extension，因此启动观测服务不会产生任何执行副作用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import unquote, urlsplit

from pickel.model_calls.content_store import ModelCallContentStore
from pickel.observe.model_call_content_reader import ModelCallContentReader
from pickel.observe.operation_fact_reader import FactStore, OperationFactReader
from pickel.observe.operation_projector import (
    OperationObservationProjector,
    project_model_call_evidence,
    project_model_calls,
)
from pickel.observe.operation_report_renderer import OperationReportRenderer
from pickel.observe.session_projector import SessionObservationProjector
from pickel.observe.trace_reader import read_operation_trace, read_trace
from pickel.observe.jsonl_trace_sink import trace_path
from pickel.shared.frozen_json import thaw_json

SCHEMA_VERSION = 1


@dataclass
class ObservationServerHandle:
    """CLI 持有的本地观测站点生命周期。"""

    server: ThreadingHTTPServer
    thread: Thread
    session_id: str

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ObservationAPI:
    """无状态 API 适配器；每次请求都从 Store 重新读取事实。"""

    def __init__(
        self,
        store: FactStore,
        content_store: ModelCallContentStore,
        *,
        session_id: str | None = None,
        trace_path_override: Path | None = None,
    ) -> None:
        self.reader = OperationFactReader(store)
        self.content_reader = ModelCallContentReader(content_store)
        self.projector = OperationObservationProjector(
            fact_reader=self.reader,
            content_reader=self.content_reader,
        )
        self.session_projector = SessionObservationProjector(
            self.reader, self.content_reader
        )
        self.session_id = session_id
        self.trace_path_override = trace_path_override

    def session(self, session_id: str) -> dict[str, Any]:
        if self.session_id is not None and session_id != self.session_id:
            raise LookupError(f"Session 不在当前观测范围内: {self.session_id}")
        value = self.reader.read_session(session_id)
        if value is None:
            raise LookupError(f"未找到 Session: {session_id}")
        # SessionObservationProjector 是 Session index 的唯一合同来源；这里
        # 不复制聚合字段，避免 API 和静态导出各自产生不一致的指标。
        path = self.trace_path_override or trace_path(session_id)
        trace = read_trace(path)
        return self.session_projector.project_session(
            session_id,
            trace_status=(trace.trace_status if trace is not None else None),
        ).to_dict()

    def operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.reader.read_operation(operation_id)
        if operation is None:
            raise LookupError(f"未找到 Operation: {operation_id}")
        if self.session_id is not None and operation.session_id != self.session_id:
            raise LookupError(f"Operation 不属于 Session: {self.session_id}")
        path = self.trace_path_override or trace_path(operation.session_id)
        trace_data = read_operation_trace(path, operation_id=operation_id)
        # Operation 页面只返回可立即渲染的结构和 ModelCall 元数据。
        # 大型 Request/Response 内容必须通过 evidence 路由按需读取。
        data = self.projector.project_operation(
            operation_id, trace_data=trace_data, include_evidence=False
        ).to_dict()
        data["schema_version"] = SCHEMA_VERSION
        data["document_evidence"] = {}
        for call in data.get("model_calls", []):
            if isinstance(call, dict):
                call["evidence_url"] = (
                    f"/api/v1/model-calls/{call.get('model_call_id', '')}/evidence"
                )
        data["links"] = {
            "self": f"/api/v1/operations/{operation_id}",
            "session": f"/api/v1/sessions/{operation.session_id}",
        }
        return data

    def render_html(self) -> str:
        """渲染工作台壳；首屏只嵌入 Session index，Operation 由前端懒加载。"""

        if self.session_id is None:
            raise ValueError("观测服务必须绑定 Session 才能提供工作台")
        session_data = self.session(self.session_id)
        operations = session_data["operations"]
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "scope": "session",
            "session": session_data["session"],
            "operation": {},
            "summary": session_data.get("aggregate", {}),
            "model_calls": [],
            "execution_nodes": [],
            "timeline": {},
            "charts": {},
            "document_evidence": {},
            "available_operations": operations,
        }
        # 相对路径让自包含导出仍保持离线可用，同时 HTTP shell 可同源懒加载。
        data["api_base"] = "."
        return OperationReportRenderer().render(data)

    def model_call_evidence(self, model_call_id: str) -> dict[str, Any]:
        call = self.reader.read_model_call(model_call_id)
        if call is None:
            raise LookupError(f"未找到 ModelCall: {model_call_id}")
        if self.session_id is not None and call.session_id != self.session_id:
            raise LookupError(f"ModelCall 不属于当前 Session: {self.session_id}")
        items = project_model_calls((call,), self.content_reader)
        if not items:
            raise LookupError(f"未找到 ModelCall: {model_call_id}")
        evidence = project_model_call_evidence(items).get(items[0].key)
        if evidence is None:
            raise LookupError(f"ModelCall 没有可用证据: {model_call_id}")
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "model_call_evidence",
            "model_call_id": model_call_id,
            # 独立 evidence 路由不会经过 OperationDocument.to_dict()；必须在
            # 传输边界显式展开 MappingProxyType/tuple，不能依赖 json.default=str
            # 把完整 Provider JSON 降级成不可导航的 Python repr 字符串。
            "evidence": thaw_json(evidence),
            "links": {"self": f"/api/v1/model-calls/{model_call_id}/evidence"},
        }


class _ObservationHandler(BaseHTTPRequestHandler):
    server_version = "PickelObserve/1"

    @property
    def api(self) -> ObservationAPI:
        return self.server.observation_api  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        request_path = urlsplit(self.path).path
        path = [unquote(part) for part in request_path.split("/")]
        try:
            if request_path in ("", "/"):
                self.send_response(HTTPStatus.OK)
                body = self.api.render_html().encode("utf-8")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; connect-src 'self'; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "base-uri 'none'; form-action 'none'",
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if (
                len(path) == 5
                and path[:3] == ["", "api", "v1"]
                and path[3] == "sessions"
            ):
                payload = self.api.session(path[4])
            elif (
                len(path) == 5
                and path[:3] == ["", "api", "v1"]
                and path[3] == "operations"
            ):
                payload = self.api.operation(path[4])
            elif (
                len(path) == 6
                and path[:3] == ["", "api", "v1"]
                and path[3] == "model-calls"
                and path[5] == "evidence"
            ):
                payload = self.api.model_call_evidence(path[4])
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    _error("not_found", "观测 API 路径不存在"),
                )
                return
        except LookupError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", str(exc)))
            return
        except (ValueError, OSError, RuntimeError) as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, _error("read_failed", str(exc))
            )
            return
        self._send_json(HTTPStatus.OK, payload)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED, _error("method_not_allowed", "仅支持 GET")
        )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # 观测服务不把访问日志污染业务 stdout；CLI 可由 HTTP 客户端自行记录。
        return


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }


def create_observation_server(
    *,
    store: FactStore,
    content_store: ModelCallContentStore,
    port: int = 8765,
    session_id: str | None = None,
    trace_path_override: Path | None = None,
) -> ThreadingHTTPServer:
    """创建但不启动服务器，便于 CLI 和测试明确控制生命周期。"""

    if not 0 <= port <= 65535:
        raise ValueError("--port 必须在 0 到 65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", port), _ObservationHandler)
    server.observation_api = ObservationAPI(  # type: ignore[attr-defined]
        store,
        content_store,
        session_id=session_id,
        trace_path_override=trace_path_override,
    )
    return server


def serve_observation(
    *,
    store: FactStore,
    content_store: ModelCallContentStore,
    port: int = 8765,
    session_id: str | None = None,
    trace_path_override: Path | None = None,
) -> None:
    """在 localhost 上阻塞运行只读观测服务。"""

    server = create_observation_server(
        store=store,
        content_store=content_store,
        port=port,
        session_id=session_id,
        trace_path_override=trace_path_override,
    )
    host, bound_port = server.server_address
    print(f"Pickel 观测服务已启动：http://{host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_observation_in_thread(
    *,
    store: FactStore,
    content_store: ModelCallContentStore,
    port: int = 0,
    session_id: str | None = None,
    trace_path_override: Path | None = None,
) -> tuple[ThreadingHTTPServer, Thread]:
    """测试与嵌入场景的非阻塞便捷入口。"""

    server = create_observation_server(
        store=store,
        content_store=content_store,
        port=port,
        session_id=session_id,
        trace_path_override=trace_path_override,
    )
    thread = Thread(target=server.serve_forever, name="pickel-observe", daemon=True)
    thread.start()
    return server, thread


def start_observation_server(
    *,
    store: FactStore,
    content_store: ModelCallContentStore,
    session_id: str,
    port: int = 0,
    trace_path_override: Path | None = None,
) -> ObservationServerHandle:
    """启动由调用方显式持有和关闭的动态观测站点。"""

    server, thread = serve_observation_in_thread(
        store=store,
        content_store=content_store,
        port=port,
        session_id=session_id,
        trace_path_override=trace_path_override,
    )
    return ObservationServerHandle(server, thread, session_id)
