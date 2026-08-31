"""进程级 Runtime 组合与活动 Conversation 管理。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion, LoadedAgentPackage
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.filesystem_blob_store import FilesystemBlobStore
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.app.boot import Boot, CompositionStore
from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_models import (
    AgentInfo,
    ConversationRequest,
    McpInspection,
    McpServerInfo,
    ModelInfo,
    ReloadResult,
)
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.config.paths import artifact_blobs_path
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.app.runtime_generation import (
    ExtensionInstance,
    LoadedPackageHandle,
    RuntimeGeneration,
)
from pickel.inbox.message import InboxMessage
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.errors import StorageConflictError
from pickel.observe.jsonl_trace_sink import (
    JsonlTraceSink,
    TraceOptions,
    trace_mode,
    trace_path,
)
from pickel.telemetry.records import observation_scope
from pickel.providers.stream import TextDelta, ThinkingDelta, ToolCallArgsDelta
from pickel.context.history_compaction import HistoryCompactionError
from pickel.runtime.history_compaction_service import HistoryCompactionService
from pickel.runtime.history_compaction_worker import (
    ModelBackedHistoryCompactionGenerator,
)
from pickel.model_calls.service import ModelCallService
from pickel.runtime.model_call_send_gate import ModelCallSendGate
from pickel.runtime.worker_call_sender import WorkerCallSender, WorkerCallSendError
from pickel.operations.operation_service import OperationService
from pickel.operations.session_operation import SessionOperation
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.delegation_service import ChildAgentSnapshot, DelegationService
from pickel.operations.agent_run_state import AgentRunError
from pickel.runtime.agent import Agent, ManualHistoryCompactionResult
from pickel.runtime.agent_registry import AgentRegistry
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.runtime_events import (
    AgentRunCompleted,
    AgentRunFailed,
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
)
from pickel.shared.conversation_mode import ConversationMode
from pickel.shared.collaboration import CollaborationState
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools

logger = logging.getLogger(__name__)


class _HeadlessTraceDriver:
    """Host 拥有的 headless 驱动观测适配；不污染 Agent 实体。"""

    def __init__(self, agent: Agent, sink: JsonlTraceSink) -> None:
        self._agent = agent
        self._sink = sink

    async def __call__(self):
        with observation_scope(self._sink):
            result = await self._agent.when_idle(
                consume_delta=self._consume_delta,
                consume_tool_event=self._sink,
            )
        self._record_result(result)
        return result

    def close(self) -> None:
        self._sink.close()

    def _consume_delta(self, delta, identity: ExecutionIdentity) -> None:
        envelope = EventEnvelope(identity=identity)
        if isinstance(delta, TextDelta):
            self._sink(TextDeltaEvent(envelope=envelope, text=delta.text))
        elif isinstance(delta, ThinkingDelta):
            self._sink(ThinkingDeltaEvent(envelope=envelope, text=delta.text))
        elif isinstance(delta, ToolCallArgsDelta):
            self._sink(
                ToolCallArgsDeltaEvent(
                    envelope=envelope, partial_json=delta.partial_json
                )
            )

    def _record_result(self, result) -> None:
        operation_result = result.operation_result
        if operation_result is None:
            return
        envelope = EventEnvelope(
            identity=ExecutionIdentity(
                session_id=self._agent.session_id,
                operation_id=operation_result.operation_id,
            )
        )
        message = operation_result.assistant_message
        if message is not None:
            text = "\n".join(
                block.text
                for block in message.content
                if isinstance(block, TextBlock) and block.text
            )
            self._sink(
                AssistantMessageEvent(
                    envelope=envelope, text=text, usage=operation_result.usage
                )
            )
        if operation_result.status == "failed":
            error = operation_result.state.error
            self._sink(
                AgentRunFailed(
                    envelope=envelope,
                    error_type=error.code if error is not None else "unknown",
                    message=error.message if error is not None else "AgentRun 失败",
                )
            )
        elif operation_result.status in {"succeeded", "cancelled"}:
            self._sink(
                AgentRunCompleted(
                    envelope=envelope,
                    usage=operation_result.usage,
                    outcome=operation_result.status,
                )
            )


class _RuntimeDelegationControl:
    """把工具的 durable acceptance 接到当前 Host 的 child activation。"""

    def __init__(self, host: "RuntimeHost", store: CompositionStore) -> None:
        self._host = host
        self._store = store

    async def start_delegation(
        self,
        *,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str,
        message: UserMessage,
        agent_id: str | None = None,
    ) -> AgentDelegation:
        delegation = DelegationService(store=self._store).start_delegation(
            parent_operation_id,
            parent_step_id,
            parent_tool_call_id,
            message,
        )
        try:
            await self._activate_session(delegation.child_session_id)
        except Exception:
            logger.exception(
                "Child Session 激活失败，将由下次启动恢复兜底: session_id=%s",
                delegation.child_session_id,
            )
        return delegation

    async def send_parent_followup(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        message: UserMessage,
    ) -> InboxMessage:
        stored = DelegationService(store=self._store).send_parent_followup(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
            target_child_session_id,
            message,
        )
        try:
            await self._activate_session(target_child_session_id)
        except Exception:
            logger.exception(
                "Child Session 激活失败，将由下次启动恢复兜底: session_id=%s",
                target_child_session_id,
            )
        return stored

    async def list_child_agents(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
    ) -> tuple[ChildAgentSnapshot, ...]:
        return DelegationService(store=self._store).list_child_agents(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
        )

    async def send_child_report(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        output: str,
    ) -> InboxMessage:
        stored = DelegationService(store=self._store).send_child_report(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
            output,
        )
        try:
            await self._activate_session(stored.session_id)
        except Exception:
            logger.exception(
                "Parent Session 激活失败，将由下次启动恢复兜底: session_id=%s",
                stored.session_id,
            )
        return stored

    async def wait_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        timeout_seconds: float,
    ):
        service = DelegationService(store=self._store)
        try:
            await self._activate_session(target_child_session_id)
        except Exception:
            logger.exception(
                "wait_delegation 激活 child 失败: session_id=%s",
                target_child_session_id,
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            snapshot = service.inspect_wait_target(
                sender_operation_id,
                sender_step_id,
                sender_tool_call_id,
                target_child_session_id,
                timeout_seconds,
            )
            if snapshot.status in {"succeeded", "failed", "cancelled", "archived"}:
                return snapshot, service.load_final_assistant(snapshot), False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return snapshot, None, True
            await asyncio.sleep(min(0.1, remaining))

    async def interrupt_agent(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None:
        operation_id = DelegationService(
            store=self._store,
        ).prepare_interrupt_agent(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
            target_child_session_id,
        )
        if operation_id is None:
            return None
        reason = f"parent_operation:{sender_operation_id}"
        operation_service = OperationService(self._store)
        accepted = operation_service.request_cancellation(operation_id, reason=reason)
        child = self._store.load_session(target_child_session_id)
        state = self._store.load_run_state(operation_id)
        if state is None:
            raise StorageConflictError("child Operation 状态在取消竞争中消失")
        if state.status in {"succeeded", "failed", "cancelled"}:
            return None
        if not accepted and state.status != "cancelling":
            accepted = operation_service.request_cancellation(
                operation_id, reason=reason
            )
            state = self._store.load_run_state(operation_id)
            if state is None:
                raise StorageConflictError("child Operation 状态在重试取消中消失")
            if state.status in {"succeeded", "failed", "cancelled"}:
                return None
            if not accepted and state.status != "cancelling":
                raise StorageConflictError("child Operation 取消 CAS 冲突")
        if (
            child is not None
            and child.active_operation_id == operation_id
            and state is not None
            and state.status == "cancelling"
        ):
            try:
                await self._activate_session(target_child_session_id)
            except Exception:
                logger.exception(
                    "Child Session 取消后激活失败，将由下次启动恢复兜底: session_id=%s",
                    target_child_session_id,
                )
        return operation_id

    async def cancel_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None:
        """旧 Package 迁移兼容入口；新 Package 使用 interrupt_agent。"""
        operation_id = DelegationService(
            store=self._store,
        ).prepare_cancel_delegation(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
            target_child_session_id,
        )
        if operation_id is None:
            return None
        reason = f"parent_operation:{sender_operation_id}"
        operation_service = OperationService(self._store)
        accepted = operation_service.request_cancellation(operation_id, reason=reason)
        state = self._store.load_run_state(operation_id)
        if state is None:
            raise StorageConflictError("child Operation 状态在取消竞争中消失")
        if state.status in {"succeeded", "failed", "cancelled"}:
            return None
        if not accepted and state.status != "cancelling":
            accepted = operation_service.request_cancellation(
                operation_id, reason=reason
            )
            state = self._store.load_run_state(operation_id)
            if state is None:
                raise StorageConflictError("child Operation 状态在重试取消中消失")
            if state.status in {"succeeded", "failed", "cancelled"}:
                return None
            if not accepted and state.status != "cancelling":
                raise StorageConflictError("child Operation 取消 CAS 冲突")
        try:
            await self._activate_session(target_child_session_id)
        except Exception:
            logger.exception(
                "Child Session 取消后激活失败，将由下次启动恢复兜底: session_id=%s",
                target_child_session_id,
            )
        return operation_id

    async def _activate_session(self, session_id: str) -> None:
        await self._host.activate_agent(session_id, self._store)
        self._host.agent_registry.wake(session_id)


class RuntimeHost:
    """进程级入口；只负责组合和替换活动 Runtime。"""

    def __init__(
        self,
        boot: Boot,
        *,
        launch_agent_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._boot = boot
        self._launch_agent_ids = launch_agent_ids
        self._extension_result = boot.extension_result or LoadResult()
        self._active_generation = self._build_generation(boot, self._extension_result)
        self._conversations: set[ConversationRuntime] = set()
        self._retired_generations: set[RuntimeGeneration] = set()
        self._agent_registry = AgentRegistry()
        self._headless_agents: dict[
            str, tuple[Agent, LoadedPackageHandle, _HeadlessTraceDriver | None]
        ] = {}
        self._operation_package_handles: dict[str, LoadedPackageHandle] = {}
        self._artifact_services: dict[int, tuple[CompositionStore, ArtifactService]] = (
            {}
        )
        self._settled_parent_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False
        self._collaboration_states: dict[str, CollaborationState] = {}

    @property
    def agent_registry(self) -> AgentRegistry:
        return self._agent_registry

    @classmethod
    async def create(
        cls,
        app_config: AppConfig,
        *,
        launch_agent_ids: tuple[str, ...] | None = None,
        boot_factory: Callable[..., Boot] = Boot.from_config,
    ) -> "RuntimeHost":
        """由 Host 统一拥有初始 Extension/Generation 的装配和失败清理。"""

        tool_bus = ToolBus()
        install_builtin_tools(tool_bus)
        result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(launch_agent_ids),
        )
        try:
            boot = boot_factory(
                app_config,
                tool_bus=tool_bus,
                extensions=result.registry,
            )
        except BaseException:
            await teardown_extensions(result, tool_bus=tool_bus)
            raise
        boot.extension_result = result
        try:
            host = cls(boot, launch_agent_ids=launch_agent_ids)
            await host._recover_runnable_sessions()
            return host
        except BaseException:
            await teardown_extensions(result, tool_bus=tool_bus)
            raise

    @property
    def boot(self) -> Boot:
        return self._boot

    @property
    def app_config(self) -> AppConfig:
        return self._boot.app_config

    def _collaboration_state(self, session_id: str) -> CollaborationState:
        return self._collaboration_states.setdefault(session_id, CollaborationState())

    def _set_collaboration_state(
        self, session_id: str, state: CollaborationState
    ) -> None:
        self._collaboration_states[session_id] = state

    @property
    def load_result(self) -> LoadResult:
        return self._extension_result

    async def _recover_runnable_sessions(self) -> None:
        """启动时只恢复 Store 明确标出的可运行 Session。"""
        store = self._boot.runtime_store()
        for session_id in store.list_runnable_session_ids():
            try:
                await self.activate_agent(session_id, store)
            except PackageLoadError as exc:
                logger.warning(
                    "启动恢复 Package 不可用，候选已隔离: session_id=%s code=%s",
                    session_id,
                    exc.code,
                )
            except Exception:
                logger.exception("启动恢复 Session 失败: session_id=%s", session_id)

    @staticmethod
    def _fail_unloadable_operation(
        *,
        store: CompositionStore,
        operation: SessionOperation,
        error: PackageLoadError,
    ) -> None:
        """精确 Package 不可装载时，以一次 revision CAS 收敛 Operation。"""

        service = OperationService(store)
        state = service.load_agent_run_state(operation.operation_id)
        if state.status in {"succeeded", "failed", "cancelled"}:
            return
        failed = replace(
            state,
            revision=state.revision + 1,
            status="failed",
            waiting_reason=None,
            current_step=None,
            error=AgentRunError(
                code=error.code,
                message=str(error),
                retryable=True,
            ),
        )
        if service.commit_state(state=failed, expected_revision=state.revision):
            return
        latest = service.load_agent_run_state(operation.operation_id)
        if latest.status not in {"succeeded", "failed", "cancelled"}:
            raise StorageConflictError(
                "精确 Package 装载失败后的 AgentRunState CAS 冲突"
            )

    def _artifact_service_for(self, store: CompositionStore) -> ArtifactService:
        """按 Store 对象复用唯一 ArtifactService 及其 BlobStore。"""
        key = id(store)
        cached = self._artifact_services.get(key)
        if cached is not None:
            cached_store, artifact_service = cached
            if cached_store is store:
                return artifact_service
        artifact_service = ArtifactService(
            artifact_store=store,
            blob_store=(
                InMemoryBlobStore()
                if isinstance(store, InMemoryRuntimeStore)
                else FilesystemBlobStore(artifact_blobs_path())
            ),
        )
        self._artifact_services[key] = (store, artifact_service)
        return artifact_service

    @staticmethod
    def _manual_history_compaction_hooks(
        *,
        agent: Agent,
        loaded: LoadedAgentPackage,
        store: CompositionStore,
        service: ConversationService,
        compaction_service: HistoryCompactionService,
    ) -> None:
        """把严格 idle 手动压缩接到当前 Agent，不经过 OperationDriver。"""

        session_id = agent.session_id
        model_policy = loaded.version.model_policy
        runtime_policy = loaded.version.runtime_policy
        worker_provider = loaded.model_clients.get("worker")
        worker_model = model_policy.worker
        worker_limit = (
            worker_model.effective_input_token_limit()
            if worker_model is not None
            else None
        )
        model_calls = ModelCallService(store)
        send_gate = ModelCallSendGate(store)
        sender = WorkerCallSender(model_calls=model_calls, send_gate=send_gate)

        def idle_check() -> bool:
            session = service.load_conversation_session(session_id)
            return (
                session.archived_at is None
                and session.active_operation_id is None
                and not store.list_pending(session_id=session_id)
            )

        async def compact():
            if worker_provider is None or worker_model is None or worker_limit is None:
                return ManualHistoryCompactionResult(
                    code="history_compaction_unavailable",
                    message="当前 LoadedAgentPackage 未配置可用 worker model",
                )
            if worker_limit < 1:
                return ManualHistoryCompactionResult(
                    code="history_compaction_worker_limit_unavailable",
                    message="无法取得当前 worker 模型的有效输入上限",
                )
            # 这里再次读取 leaf；Agent 已经持有 drive lock，期间 Inbox 只能
            # 留在 pending，不能被 AgentDriver claim。
            session = service.load_conversation_session(session_id)
            try:
                node = await compaction_service.compact(
                    session_id=session_id,
                    expected_leaf_node_id=session.active_node_id,
                    model_context=None,
                    send_summarizer=lambda **kwargs: sender(
                        session_id=session_id,
                        context=kwargs["context"],
                        purpose=kwargs["purpose"],
                        worker_provider=worker_provider,
                        runtime_policy=runtime_policy,
                        provider_timeout_seconds=600.0,
                    ),
                    max_summary_tokens=runtime_policy.compaction_max_summary_tokens,
                    preserve_tail_tokens=runtime_policy.compaction_tail_tokens,
                    worker_input_limit=worker_limit,
                )
            except HistoryCompactionError as exc:
                return ManualHistoryCompactionResult(exc.code, str(exc))
            except WorkerCallSendError as exc:
                return ManualHistoryCompactionResult(
                    "worker_send_failed", f"worker 压缩调用失败：{exc}"
                )
            except RuntimeError as exc:
                if "leaf CAS 冲突" in str(exc):
                    return ManualHistoryCompactionResult(
                        "history_compaction_leaf_conflict",
                        "压缩 leaf 已变化，拒绝把旧摘要挂到新历史上",
                    )
                return ManualHistoryCompactionResult(
                    "history_compaction_failed", f"手动历史压缩失败：{exc}"
                )
            except Exception as exc:  # noqa: BLE001 — 手动入口返回稳定错误
                return ManualHistoryCompactionResult(
                    "history_compaction_failed", f"手动历史压缩失败：{exc}"
                )
            return ManualHistoryCompactionResult(
                code="ok", message="历史压缩完成", node_id=node.node_id
            )

        agent.configure_manual_history_compaction(
            compactor=compact, idle_check=idle_check
        )

    def _schedule_settled_parent_wake(
        self, store: CompositionStore, session_id: str
    ) -> None:
        """终态提交后确保 Parent 已装配，再交给 Registry 唤醒。"""

        if self._shutting_down:
            return
        existing = self._settled_parent_tasks.get(session_id)
        if existing is not None and not existing.done():
            return

        async def activate_and_wake() -> None:
            try:
                await self.activate_agent(session_id, store)
                self._agent_registry.wake(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "settled Parent 激活失败，将由下次启动恢复兜底: session_id=%s",
                    session_id,
                )

        task = asyncio.create_task(activate_and_wake())
        self._settled_parent_tasks[session_id] = task
        task.add_done_callback(
            lambda completed: self._settled_parent_task_finished(session_id, completed)
        )

    def _settled_parent_task_finished(
        self, session_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._settled_parent_tasks.get(session_id) is task:
            self._settled_parent_tasks.pop(session_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("settled Parent 唤醒任务异常: session_id=%s", session_id)

    def _load_operation_package(
        self,
        operation: SessionOperation,
        *,
        generation: RuntimeGeneration,
        boot: Boot,
        store: CompositionStore,
        artifact_service: ArtifactService,
        expected_agent_id: str,
    ) -> LoadedAgentPackage:
        """为非终态 Operation 获取并保留精确 Generation 引用。"""

        existing = self._operation_package_handles.get(operation.operation_id)
        if existing is not None:
            if existing.package_version_id != operation.agent_package_version_id:
                raise RuntimeError("Operation Package 引用与持久化版本不一致")
            package = existing.package
            if package.version.agent_id != expected_agent_id:
                raise RuntimeError("Operation Package 引用与 Agent 不一致")
            return package

        loaded = boot.load_agent_package(
            operation.agent_package_version_id,
            store=store,
            artifact_service=artifact_service,
            expected_agent_id=expected_agent_id,
        )
        loaded = generation.cache_loaded_package(
            loaded.version.package_version_id,
            loaded,
        )
        handle = generation.acquire_loaded_package(loaded.version.package_version_id)
        self._operation_package_handles[operation.operation_id] = handle
        return handle.package

    def _resolve_operation_effects(
        self,
        operation: SessionOperation,
        *,
        generation: RuntimeGeneration,
        boot: Boot,
        store: CompositionStore,
        artifact_service: ArtifactService,
        expected_agent_id: str,
        delegation_control: _RuntimeDelegationControl,
    ) -> RuntimeEffects:
        loaded = self._load_operation_package(
            operation,
            generation=generation,
            boot=boot,
            store=store,
            artifact_service=artifact_service,
            expected_agent_id=expected_agent_id,
        )
        handle = self._operation_package_handles[operation.operation_id]
        owner_boot = handle.generation.boot
        if owner_boot is None:
            raise RuntimeError("Operation 所属 RuntimeGeneration 缺少 Boot")
        (
            allowed_tool_names,
            parent_allowed_root,
        ) = self._delegated_boundaries(store, operation.session_id)
        return owner_boot._build_effects(
            loaded_agent_package=loaded,
            artifact_service=artifact_service,
            session_cwd=Path(operation.workspace_binding.working_directory),
            delegation_control=delegation_control,
            allowed_tool_names=allowed_tool_names,
            parent_allowed_root=parent_allowed_root,
        )

    def _delegated_boundaries(
        self, store: CompositionStore, session_id: str
    ) -> tuple[frozenset[str] | None, Path | None]:
        """沿父子图求有效 Tool 集；工作区只读取冻结 WorkspaceBinding。"""
        current_session_id = session_id
        allowed_tool_names: frozenset[str] | None = None
        parent_allowed_root: Path | None = None
        seen: set[str] = set()
        while current_session_id not in seen:
            seen.add(current_session_id)
            delegation = store.load_delegation(current_session_id)
            if delegation is None:
                return allowed_tool_names, parent_allowed_root
            parent_operation = store.load_operation(delegation.parent_operation_id)
            if parent_operation is None:
                raise ValueError("Delegation parent Operation 不存在")
            parent_package = store.load_agent_package_version(
                parent_operation.agent_package_version_id
            )
            if parent_package is None:
                raise ValueError("Delegation parent Package 不存在")
            package_tools = frozenset(tool.name for tool in parent_package.tools)
            allowed_tool_names = (
                package_tools
                if allowed_tool_names is None
                else allowed_tool_names.intersection(package_tools)
            )
            if parent_allowed_root is None:
                parent_allowed_root = parent_operation.workspace_binding.allowed_root
            current_session_id = parent_operation.session_id
        raise ValueError("Delegation parent 链存在环")

    def _resolve_delegation_package(
        self,
        *,
        boot: Boot,
        store: CompositionStore,
        operation: SessionOperation,
        parent_package: AgentPackageVersion,
        target_agent_id: str,
    ) -> AgentPackageVersion:
        """在 DelegateAgentIntent 提交前冻结目标 Agent 的 format 3 Package。"""
        if target_agent_id not in boot.app_config.agents:
            raise ValueError(f"Delegation 目标 Agent 未注册: {target_agent_id}")
        child_package = boot._agent_package_builder.build_agent_package_version(
            target_agent_id
        )
        if child_package.format_version != 3:
            raise ValueError("Delegation child Package 必须是 format 3")
        store.insert_agent_package_version(child_package)
        return child_package

    async def _release_operation_package(self, operation: SessionOperation) -> None:
        handle = self._operation_package_handles.pop(operation.operation_id, None)
        generations: set[RuntimeGeneration] = set()
        if handle is not None:
            generations.add(handle.generation)
            await handle.close()

        for generation in generations:
            if generation.closed:
                self._retired_generations.discard(generation)

    @property
    def active_generation(self) -> RuntimeGeneration:
        return self._active_generation

    async def activate_agent(self, session_id: str, store: CompositionStore) -> Agent:
        """为 durable Session 装配 headless Agent 并幂等唤醒。"""
        existing = self._headless_agents.get(session_id)
        if existing is not None:
            return existing[0]
        registered = self._agent_registry.get(session_id)
        if registered is not None:
            return registered
        session = store.load_session(session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        if session.archived_at is not None:
            raise ValueError("归档 Session 不能激活 Agent")
        boot = self._active_generation_boot()
        generation = self._active_generation
        artifact_service = self._artifact_service_for(store)
        package_id: str | None = None
        delegation = store.load_delegation(session_id)
        parent_operation = None
        if delegation is not None:
            parent_operation = store.load_operation(delegation.parent_operation_id)
            if parent_operation is None:
                raise ValueError("Delegation parent Operation 不存在")
        if session.active_operation_id is not None:
            operation = store.load_operation(session.active_operation_id)
            if operation is None or operation.session_id != session_id:
                raise ValueError("Session.active_operation_id 指向无效 Operation")
            package_id = operation.agent_package_version_id
        else:
            if delegation is not None:
                package_id = delegation.child_package_version_id
        (
            allowed_tool_names,
            parent_allowed_root,
        ) = self._delegated_boundaries(store, session_id)
        if session.active_operation_id is not None:
            try:
                loaded = self._load_operation_package(
                    operation,
                    generation=generation,
                    boot=boot,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=session.agent_id,
                )
            except PackageLoadError as exc:
                self._fail_unloadable_operation(
                    store=store,
                    operation=operation,
                    error=exc,
                )
                raise
            else:
                operation_handle = self._operation_package_handles[
                    operation.operation_id
                ]
                if operation_handle.generation is not generation:
                    # Agent 外壳属于当前代；实际 Operation Effects 仍从旧 lease
                    # 解析，不能把旧 Package 对象塞进新代缓存并重复关闭。
                    try:
                        loaded = boot.load_agent_package(
                            operation.agent_package_version_id,
                            store=store,
                            artifact_service=artifact_service,
                            expected_agent_id=session.agent_id,
                        )
                    except PackageLoadError:
                        if delegation is not None:
                            raise
                        loaded = boot.resolve_loaded_agent_package(
                            session.agent_id, artifact_service=artifact_service
                        )
        elif package_id is None:
            loaded = boot.resolve_loaded_agent_package(
                session.agent_id, artifact_service=artifact_service
            )
        else:
            try:
                loaded = boot.load_agent_package(
                    package_id,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=session.agent_id,
                )
            except PackageLoadError:
                if session.active_operation_id is None:
                    raise
                # Agent 外壳可先使用当前包；OperationDriver 仍会按 Operation
                # 绑定的历史包精确加载，并把缺失记录为可恢复失败。
                loaded = boot.resolve_loaded_agent_package(
                    session.agent_id, artifact_service=artifact_service
                )
        if allowed_tool_names is not None and not any(
            tool.name in allowed_tool_names for tool in loaded.version.tools
        ):
            raise ValueError("Delegation Parent 与 child 的 Tool 权限交集为空")
        if loaded.version.agent_id != session.agent_id:
            raise ValueError("LoadedAgentPackage.agent_id 与 Session 不匹配")
        loaded = generation.cache_loaded_package(
            loaded.version.package_version_id, loaded
        )
        handle = generation.acquire_loaded_package(loaded.version.package_version_id)
        agent: Agent | None = None
        trace_sink: JsonlTraceSink | None = None
        registered = False
        try:
            trace_sink = self._open_headless_trace(session_id)
            agent = boot.build_agent(
                store=store,
                session_id=session_id,
                loaded_agent_package=loaded,
                artifact_service=artifact_service,
                session_cwd=session.cwd,
                operation_service=OperationService(store),
                wake_callback=self._agent_registry.wake,
                terminal_callback=lambda parent_session_id: self._schedule_settled_parent_wake(
                    store, parent_session_id
                ),
                delegation_control=(
                    delegation_control := _RuntimeDelegationControl(self, store)
                ),
                delegation_package_resolver=(
                    lambda operation, parent_package, target_agent_id: self._resolve_delegation_package(
                        boot=boot,
                        store=store,
                        operation=operation,
                        parent_package=parent_package,
                        target_agent_id=target_agent_id,
                    )
                ),
                allowed_tool_names=allowed_tool_names,
                parent_allowed_root=parent_allowed_root,
                operation_package_loader=lambda operation: self._load_operation_package(
                    operation,
                    generation=generation,
                    boot=boot,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=session.agent_id,
                ),
                operation_effects_resolver=lambda operation: self._resolve_operation_effects(
                    operation,
                    generation=generation,
                    boot=boot,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=session.agent_id,
                    delegation_control=delegation_control,
                ),
                collaboration_state_provider=lambda _session_id: self._collaboration_state(
                    session_id
                ),
                release_operation_package=self._release_operation_package,
            )
            trace_driver = (
                _HeadlessTraceDriver(agent, trace_sink)
                if trace_sink is not None and isinstance(agent, Agent)
                else None
            )
            if trace_sink is not None and trace_driver is None:
                trace_sink.close()
                trace_sink = None
            self._agent_registry.register(
                agent,
                drive=trace_driver if trace_driver is not None else None,
            )
            registered = True
            store.insert_agent_package_version(loaded.version)
            self._headless_agents[session_id] = (agent, handle, trace_driver)
            self._agent_registry.wake(session_id)
            return agent
        except BaseException:
            self._headless_agents.pop(session_id, None)
            if registered and agent is not None:
                self._agent_registry.unregister(session_id, agent)
            if trace_sink is not None:
                trace_sink.close()
            await handle.close()
            raise

    def _open_headless_trace(self, session_id: str) -> JsonlTraceSink | None:
        configured = self.app_config.observability.trace
        mode = trace_mode(configured.mode)
        if mode == "off":
            return None
        try:
            return JsonlTraceSink(
                trace_path(session_id),
                TraceOptions(
                    mode=mode,
                    queue_capacity=configured.queue_capacity,
                    batch_size=configured.batch_size,
                    flush_interval_ms=configured.flush_interval_ms,
                    max_file_size_mb=configured.max_file_size_mb,
                    max_age_days=configured.max_age_days,
                    max_total_size_mb=configured.max_total_size_mb,
                ),
            )
        except OSError:
            return None

    def list_agents(self) -> tuple[AgentInfo, ...]:
        return tuple(
            AgentInfo(agent_id=item) for item in sorted(self.app_config.agents)
        )

    def list_models(self) -> tuple[ModelInfo, ...]:
        return tuple(
            ModelInfo(provider=provider, model=model)
            for provider in sorted(self.app_config.providers)
            for model in sorted(self.app_config.providers[provider].models)
        )

    def inspect_mcp(self, conversation: ConversationRuntime) -> McpInspection:
        generation = conversation.runtime_generation or self._active_generation
        catalog = generation.extension_catalog
        source = catalog.mcp_status_source
        if source is None:
            return McpInspection(available=False)
        snapshot = source.snapshot()
        active_by_server: dict[str, int] = {}
        for tool in conversation.list_tools():
            if tool.source == "mcp" and tool.origin is not None:
                active_by_server[tool.origin] = active_by_server.get(tool.origin, 0) + 1
        return McpInspection(
            available=True,
            servers=tuple(
                McpServerInfo(
                    name=server.name,
                    status=server.status,
                    transport=server.transport,
                    config_scope=server.config_scope,
                    protocol_version=server.protocol_version,
                    implementation=_implementation_label(
                        server.implementation_name,
                        server.implementation_version,
                    ),
                    discovered_tools=server.discovered_tools,
                    active_tools=active_by_server.get(server.name, 0),
                    last_error=server.last_error,
                )
                for server in snapshot.servers
            ),
            diagnostics=snapshot.diagnostics,
        )

    def open_conversation(self, request: ConversationRequest) -> ConversationRuntime:
        if request.session_id is not None:
            existing = self._find_open_conversation(request.session_id)
            if existing is not None:
                return existing
        generation = self._active_generation
        boot = self._active_generation_boot()
        if request.persistence == "ephemeral":
            store: CompositionStore = InMemoryRuntimeStore()
        else:
            store = boot.runtime_store()
        service = boot.build_conversation_service(store=store)
        if request.session_id is not None:
            session = service.load_conversation_session(request.session_id)
            agent_id = session.agent_id
        else:
            loaded = boot.resolve_loaded_agent_package(
                request.agent_id,
                artifact_service=self._artifact_service_for(store),
            )
            agent_id = loaded.version.agent_id
            session = service.create_conversation_session(
                agent_id=agent_id,
                cwd=str((request.cwd or Path.cwd()).resolve()),
            )
        return self._attach(
            boot=boot,
            generation=generation,
            store=store,
            service=service,
            session=session,
            agent_id=agent_id,
            persistence=request.persistence,
            mode=request.mode,
        )

    def _find_open_conversation(self, session_id: str) -> ConversationRuntime | None:
        return next(
            (
                conversation
                for conversation in self._conversations
                if conversation.session.session_id == session_id
                and not conversation.closed
            ),
            None,
        )

    def new_session(self, conversation: ConversationRuntime) -> ConversationRuntime:
        next_conversation = self.open_conversation(
            ConversationRequest(
                agent_id=conversation.agent_definition.agent_id,
                persistence=conversation.persistence,
                cwd=Path.cwd(),
                mode=conversation.mode,
            )
        )
        conversation.detach()
        return next_conversation

    def switch_agent(
        self,
        conversation: ConversationRuntime,
        agent_id: str,
    ) -> ConversationRuntime:
        if agent_id not in self.app_config.agents:
            raise KeyError(f"Unknown agent: {agent_id}")
        next_conversation = self.open_conversation(
            ConversationRequest(
                agent_id=agent_id,
                persistence=conversation.persistence,
                cwd=Path.cwd(),
                mode=conversation.mode,
            )
        )
        conversation.detach()
        return next_conversation

    async def reload(
        self,
        conversation: ConversationRuntime,
        *,
        app_config: AppConfig | None = None,
        boot_factory: Callable[..., Boot] = Boot.from_config,
    ) -> ReloadResult:
        """构建新 Generation 后原子替换活动代；失败时旧代继续服务。"""
        app_config = app_config or Config.load(cwd=Path.cwd())
        tool_bus = ToolBus()
        install_builtin_tools(tool_bus)
        extension_result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(self._launch_agent_ids),
        )
        try:
            next_boot = boot_factory(
                app_config,
                tool_bus=tool_bus,
                extensions=extension_result.registry,
            )
            next_boot.extension_result = extension_result
            next_generation = self._build_generation(next_boot, extension_result)
            store = (
                conversation.persistence_store
                if conversation.persistence == "ephemeral"
                else next_boot.runtime_store()
            )
            next_conversation = self._attach(
                boot=next_boot,
                generation=next_generation,
                store=store,
                service=next_boot.build_conversation_service(store=store),
                session=conversation.session,
                agent_id=conversation.agent_definition.agent_id,
                persistence=conversation.persistence,
                mode=conversation.mode,
                replace_agent=True,
            )
        except BaseException:
            # 新代尚未发布，任何构建/attach 失败都只回滚新资源。
            next_generation = locals().get("next_generation")
            if next_generation is not None:
                next_generation.retire()
                await next_generation.close()
            else:
                await teardown_extensions(extension_result, tool_bus=tool_bus)
            raise

        old_generation = self._active_generation
        self._active_generation = next_generation
        self._boot = next_boot
        self._extension_result = extension_result
        self._conversations.discard(conversation)
        self._conversations.add(next_conversation)
        old_generation.retire()
        self._retired_generations.add(old_generation)
        conversation.detach()
        # 有其他 Conversation/非终态 Operation 时旧代必须继续存活，reload
        # 不能被它们阻塞；无引用时等待已安排的清理，保证 reload 的短代完整收口。
        if old_generation.can_close:
            await old_generation.wait_closed()
            self._retired_generations.discard(old_generation)
        return ReloadResult(
            conversation=next_conversation,
            warnings=tuple(str(item) for item in extension_result.errors),
        )

    def _attach(
        self,
        *,
        boot: Boot,
        generation: RuntimeGeneration,
        store: CompositionStore,
        service: ConversationService,
        session: ConversationSession,
        agent_id: str,
        persistence: str,
        mode: ConversationMode,
        replace_agent: bool = False,
    ) -> ConversationRuntime:
        # 同一个 OperationService 同时服务 AgentDriver 与 UI Adapter，避免
        # ConversationRuntime 绕过窄服务直接读取 Operation 状态。
        operation_service = OperationService(store)
        artifact_service = self._artifact_service_for(store)
        delegation = store.load_delegation(session.session_id)
        (
            allowed_tool_names,
            parent_allowed_root,
        ) = self._delegated_boundaries(store, session.session_id)
        if session.active_operation_id is not None:
            try:
                operation = operation_service.load_operation(
                    session.active_operation_id
                )
            except LookupError:
                raise ValueError(
                    "Session.active_operation_id 指向不存在的 Operation: "
                    f"{session.active_operation_id}"
                )
            if operation.session_id != session.session_id:
                raise ValueError("Operation 与 Session 不匹配")
            try:
                loaded = self._load_operation_package(
                    operation,
                    generation=generation,
                    boot=boot,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=agent_id,
                )
            except PackageLoadError:
                if delegation is not None:
                    raise
                # Host 仍需要构建 Agent，由 OperationDriver 将同一装载
                # 失败以 revision CAS 持久化为 retryable failed。此处的
                # 当前 Package 只用于组合 UI/Host，不会执行旧 Operation。
                loaded = boot.resolve_loaded_agent_package(
                    agent_id, artifact_service=artifact_service
                )
            else:
                operation_handle = self._operation_package_handles[
                    operation.operation_id
                ]
                if operation_handle.generation is not generation:
                    # UI/Host 外壳使用当前代的独立 Package；Operation 本身继续
                    # 通过旧代 lease 执行。
                    try:
                        loaded = boot.load_agent_package(
                            operation.agent_package_version_id,
                            store=store,
                            artifact_service=artifact_service,
                            expected_agent_id=agent_id,
                        )
                    except PackageLoadError:
                        if delegation is not None:
                            raise
                        loaded = boot.resolve_loaded_agent_package(
                            agent_id, artifact_service=artifact_service
                        )
        else:
            if delegation is None:
                loaded = boot.resolve_loaded_agent_package(
                    agent_id, artifact_service=artifact_service
                )
            else:
                parent_operation = store.load_operation(delegation.parent_operation_id)
                if parent_operation is None:
                    raise ValueError("Delegation parent Operation 不存在")
                package_id = delegation.child_package_version_id
                loaded = boot.load_agent_package(
                    package_id,
                    store=store,
                    artifact_service=artifact_service,
                    expected_agent_id=agent_id,
                )
        loaded = generation.cache_loaded_package(
            loaded.version.package_version_id,
            loaded,
        )
        package_handle = generation.acquire_loaded_package(
            loaded.version.package_version_id
        )
        history_compaction_service = HistoryCompactionService(
            service, ModelBackedHistoryCompactionGenerator()
        )
        previous_agent = self._agent_registry.get(session.session_id)
        try:
            store.insert_agent_package_version(loaded.version)
            if previous_agent is not None and not replace_agent:
                agent = previous_agent
            else:
                agent = boot.build_agent(
                    store=store,
                    session_id=session.session_id,
                    loaded_agent_package=loaded,
                    artifact_service=artifact_service,
                    session_cwd=session.cwd,
                    operation_service=operation_service,
                    wake_callback=self._agent_registry.wake,
                    terminal_callback=lambda parent_session_id: self._schedule_settled_parent_wake(
                        store, parent_session_id
                    ),
                    delegation_control=(
                        delegation_control := _RuntimeDelegationControl(self, store)
                    ),
                    delegation_package_resolver=(
                        lambda operation, parent_package, target_agent_id: self._resolve_delegation_package(
                            boot=boot,
                            store=store,
                            operation=operation,
                            parent_package=parent_package,
                            target_agent_id=target_agent_id,
                        )
                    ),
                    allowed_tool_names=allowed_tool_names,
                    parent_allowed_root=parent_allowed_root,
                    operation_package_loader=lambda operation: self._load_operation_package(
                        operation,
                        generation=generation,
                        boot=boot,
                        store=store,
                        artifact_service=artifact_service,
                        expected_agent_id=agent_id,
                    ),
                    operation_effects_resolver=lambda operation: self._resolve_operation_effects(
                        operation,
                        generation=generation,
                        boot=boot,
                        store=store,
                        artifact_service=artifact_service,
                        expected_agent_id=agent_id,
                        delegation_control=delegation_control,
                    ),
                    collaboration_state_provider=lambda _session_id: self._collaboration_state(
                        session.session_id
                    ),
                    release_operation_package=self._release_operation_package,
                    history_compaction_service=history_compaction_service,
                )
                self._manual_history_compaction_hooks(
                    agent=agent,
                    loaded=loaded,
                    store=store,
                    service=service,
                    compaction_service=history_compaction_service,
                )
            conversation = ConversationRuntime(
                loaded_agent_package=loaded,
                loaded_package_handle=package_handle,
                agent=agent,
                session=session,
                conversation_service=service,
                operation_service=operation_service,
                persistence_store=store,
                persistence=persistence,
                app_config=boot.app_config,
                mode=mode,
                collaboration_state=self._collaboration_state(session.session_id),
                on_collaboration_state_change=lambda state: self._set_collaboration_state(
                    session.session_id, state
                ),
                on_detach=lambda: self._agent_registry.unregister(
                    session.session_id,
                    agent,
                ),
            )
        except BaseException:
            package_handle.close_sync()
            raise
        try:
            self._add_event_processors(conversation, registry=boot.extensions)
        except BaseException:
            conversation.detach()
            raise
        if previous_agent is None:
            try:
                self._agent_registry.register(agent)
            except BaseException:
                conversation.detach()
                raise
        elif replace_agent:
            self._agent_registry.unregister(session.session_id, previous_agent)
            try:
                self._agent_registry.register(agent)
            except BaseException:
                self._agent_registry.register(previous_agent)
                conversation.detach()
                raise
        headless = self._headless_agents.get(session.session_id)
        if headless is not None and headless[0] is previous_agent:
            # Conversation Handle 已接管 live Agent；移除 Host 的额外常驻引用。
            self._headless_agents.pop(session.session_id, None)
            if headless[2] is not None:
                headless[2].close()
            headless[1].close_sync()
        self._conversations.add(conversation)
        return conversation

    @staticmethod
    def _add_event_processors(
        conversation: ConversationRuntime,
        *,
        registry: Any,
    ) -> None:
        context = ConversationExtensionContext(
            agent_id=conversation.agent_definition.agent_id,
            session_id=conversation.session.session_id,
            mode=conversation.mode,
            publish_output=conversation.publish_output,
            start_background_task=conversation.start_background_task,
        )
        for resolved in registry.resolve_event_processors(context):
            conversation.add_event_processor(resolved.processor, resolved.event_types)

    async def shutdown(self) -> None:
        self._shutting_down = True
        settled_tasks = tuple(self._settled_parent_tasks.values())
        for task in settled_tasks:
            if not task.done():
                task.cancel()
        if settled_tasks:
            await asyncio.gather(*settled_tasks, return_exceptions=True)
        self._settled_parent_tasks.clear()
        await self._agent_registry.shutdown()
        for _session_id, (_agent, handle, trace_driver) in tuple(
            self._headless_agents.items()
        ):
            if trace_driver is not None:
                trace_driver.close()
            await handle.close()
        self._headless_agents.clear()
        for conversation in tuple(self._conversations):
            conversation.detach()
        self._conversations.clear()
        for operation_id, handle in tuple(self._operation_package_handles.items()):
            await handle.close()
            self._operation_package_handles.pop(operation_id, None)
        generation = self._active_generation
        if not generation.closed and not generation.retired:
            generation.retire()
        generations = (generation, *tuple(self._retired_generations))
        for retired in generations:
            if retired.can_close:
                await retired.wait_closed()
        self._retired_generations = {
            retired for retired in self._retired_generations if not retired.closed
        }
        self._artifact_services.clear()

    def _active_generation_boot(self) -> Boot:
        # RuntimeGeneration 的所有贡献属于随代切换的 Boot；这里单独方法让
        # Conversation/inspect 路径不会误读已经 retired 的 Boot。
        return self._boot

    @staticmethod
    def _build_generation(boot: Boot, result: LoadResult) -> RuntimeGeneration:
        generation = RuntimeGeneration(
            f"generation_{uuid4().hex}",
            extension_catalog=boot.extensions,
            boot=boot,
        )
        for extension_id, host in result.hosts.items():
            generation.add_extension(
                ExtensionInstance(
                    extension_id,
                    generation.generation_id,
                    host.scope,
                    extension_version=host.extension_version,
                )
            )
        generation.publish()
        return generation


def _implementation_label(name: str | None, version: str | None) -> str | None:
    if name and version:
        return f"{name} {version}"
    return name or version
