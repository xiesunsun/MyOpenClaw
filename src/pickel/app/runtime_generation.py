"""RuntimeGeneration 及其生命周期引用。

本模块只描述一代 Runtime 的所有权边界，不负责构建 Extension、解析配置或
切换 Host 中的 active generation。那些动作由 RuntimeHost 在后续批次接线。
"""

from __future__ import annotations

import inspect
import logging
import threading
import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from pickel.agents.agent_package import ExtensionVersion

if TYPE_CHECKING:
    from pickel.app.boot import Boot

logger = logging.getLogger(__name__)


class ContributionScopeProtocol(Protocol):
    """RuntimeGeneration 所需的最窄贡献清理接口。"""

    async def close(self) -> None:
        """逆序撤销该 Scope 拥有的贡献和资源。"""


class RuntimeGenerationState(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"
    CLOSED = "closed"


class ExtensionInstanceState(StrEnum):
    LOADING = "loading"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


RuntimeGenerationId: TypeAlias = str
ExtensionId: TypeAlias = str
PackageVersionId: TypeAlias = str


class RuntimeGenerationStateError(RuntimeError):
    """RuntimeGeneration 或其 ExtensionInstance 的非法生命周期转换。"""


class _EmptyScope:
    async def close(self) -> None:
        return None


async def _close_resource(resource: Any) -> None:
    """调用窄资源关闭接口，兼容同步和异步 close。"""

    close = getattr(resource, "close", None)
    if close is None:
        close = getattr(resource, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class ExtensionInstance:
    """某个 Extension 在某个 RuntimeGeneration 中的一次装载。"""

    def __init__(
        self,
        extension_id: ExtensionId,
        generation_id: RuntimeGenerationId,
        scope: ContributionScopeProtocol | None = None,
        state: ExtensionInstanceState = ExtensionInstanceState.LOADING,
        extension_version: ExtensionVersion | None = None,
    ) -> None:
        if not extension_id:
            raise ValueError("extension_id 不能为空")
        if not generation_id:
            raise ValueError("generation_id 不能为空")
        if state not in {
            ExtensionInstanceState.LOADING,
            ExtensionInstanceState.ACTIVE,
            ExtensionInstanceState.CLOSING,
            ExtensionInstanceState.CLOSED,
        }:
            raise ValueError(f"未知 ExtensionInstance 状态: {state!r}")
        self.extension_id = extension_id
        self.generation_id = generation_id
        self.scope = scope or _EmptyScope()
        if (
            extension_version is not None
            and extension_version.extension_id != extension_id
        ):
            raise ValueError("ExtensionVersion 与 ExtensionInstance 身份不一致")
        self.extension_version = extension_version
        self.state = state
        self._close_lock = threading.RLock()
        self._close_task: asyncio.Task[None] | None = None

    def activate(self) -> None:
        """将已完成 setup 的实例发布为 active。"""

        with self._close_lock:
            if self.state is not ExtensionInstanceState.LOADING:
                raise RuntimeGenerationStateError(
                    f"Extension '{self.extension_id}' 不能从 {self.state.value} 激活"
                )
            self.state = ExtensionInstanceState.ACTIVE

    async def close(self) -> None:
        """关闭实例 Scope；重复调用无副作用。"""

        with self._close_lock:
            if self.state is ExtensionInstanceState.CLOSED:
                return
            task = self._close_task
            if task is None:
                self.state = ExtensionInstanceState.CLOSING
                task = asyncio.create_task(self._close_scope())
                self._close_task = task
        await asyncio.shield(task)

    async def _close_scope(self) -> None:
        try:
            await _close_resource(self.scope)
        except Exception:
            # 关闭流程必须继续向后清理；Scope 自己也应遵守同一约束。
            logger.exception("关闭 Extension '%s' 失败", self.extension_id)
        finally:
            with self._close_lock:
                self.state = ExtensionInstanceState.CLOSED


class LoadedPackageHandle:
    """Operation 对 Generation 内 LoadedAgentPackage 的精确引用。"""

    def __init__(
        self,
        generation: "RuntimeGeneration",
        package_version_id: PackageVersionId,
        package: Any,
    ) -> None:
        self._generation = generation
        self.package_version_id = package_version_id
        self.package = package
        self._closed = False
        self._close_lock = threading.RLock()

    @property
    def generation(self) -> "RuntimeGeneration":
        return self._generation

    @property
    def generation_id(self) -> RuntimeGenerationId:
        return self._generation.generation_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        await self._generation._release_package(self.package_version_id)

    def close_sync(self) -> None:
        """同步生命周期（例如 ConversationRuntime.detach）释放引用。"""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._generation._release_package_sync(self.package_version_id)

    async def __aenter__(self) -> "LoadedPackageHandle":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()


class RuntimeGeneration:
    """一代完整可执行环境及其共享资源所有者。

    ``publish`` 是唯一的 building → active 入口；``retire`` 只允许 active →
    retired。旧代不会因为新代发布而立即销毁，直到 Operation 和 Package handle
    都释放后才会逆序关闭 Extension 与 Generation Scope。
    """

    def __init__(
        self,
        generation_id: RuntimeGenerationId,
        *,
        state: RuntimeGenerationState = RuntimeGenerationState.BUILDING,
        scope: ContributionScopeProtocol | None = None,
        extension_instances: Mapping[ExtensionId, ExtensionInstance] | None = None,
        extension_catalog: Mapping[str, Any] | Any = (),
        loaded_packages: Mapping[PackageVersionId, Any] | None = None,
        boot: "Boot | None" = None,
    ) -> None:
        if not generation_id:
            raise ValueError("generation_id 不能为空")
        self.generation_id = generation_id
        if state not in {
            RuntimeGenerationState.BUILDING,
            RuntimeGenerationState.ACTIVE,
            RuntimeGenerationState.RETIRED,
            RuntimeGenerationState.CLOSED,
        }:
            raise ValueError(f"未知 RuntimeGeneration 状态: {state!r}")
        self.state = state
        self.scope = scope or _EmptyScope()
        self.extension_catalog = extension_catalog
        self.boot = boot
        self.extension_instances: dict[ExtensionId, ExtensionInstance] = dict(
            extension_instances or {}
        )
        self.loaded_packages: dict[PackageVersionId, Any] = dict(loaded_packages or {})
        self.operation_ref_count = 0
        self._package_ref_counts: dict[PackageVersionId, int] = {
            package_id: 0 for package_id in self.loaded_packages
        }
        self._lock = threading.RLock()
        self._close_task: Any = None

        for extension in self.extension_instances.values():
            if extension.generation_id != generation_id:
                raise ValueError(
                    f"Extension '{extension.extension_id}' 不属于 Generation "
                    f"'{generation_id}'"
                )

    @property
    def closed(self) -> bool:
        return self.state is RuntimeGenerationState.CLOSED

    @property
    def retired(self) -> bool:
        return self.state is RuntimeGenerationState.RETIRED

    @property
    def can_close(self) -> bool:
        return (
            self.state is RuntimeGenerationState.RETIRED
            and self.operation_ref_count == 0
        )

    def add_extension(self, instance: ExtensionInstance) -> None:
        """在 building 阶段加入一个尚未发布的 ExtensionInstance。"""

        with self._lock:
            self._require_state(RuntimeGenerationState.BUILDING, "加入 Extension")
            if instance.generation_id != self.generation_id:
                raise ValueError(
                    f"Extension '{instance.extension_id}' 不属于 Generation "
                    f"'{self.generation_id}'"
                )
            if instance.extension_id in self.extension_instances:
                raise ValueError(f"Extension 已存在: {instance.extension_id}")
            self.extension_instances[instance.extension_id] = instance

    def add_loaded_package(
        self,
        package_version_id: PackageVersionId,
        package: Any,
    ) -> None:
        """在 building 阶段加入共享 LoadedAgentPackage。"""

        if not package_version_id:
            raise ValueError("package_version_id 不能为空")
        with self._lock:
            self._require_state(
                RuntimeGenerationState.BUILDING, "加入 LoadedAgentPackage"
            )
            if package_version_id in self.loaded_packages:
                raise ValueError(f"LoadedAgentPackage 已存在: {package_version_id}")
            self.loaded_packages[package_version_id] = package
            self._package_ref_counts[package_version_id] = 0

    def cache_loaded_package(
        self,
        package_version_id: PackageVersionId,
        package: Any,
    ) -> Any:
        """把构建好的 Package 放入当前 Generation 的共享缓存。

        Package 缓存属于 Generation；reload 后不会从旧 Generation 借用同名
        Package。相同版本重复构建时保留第一次缓存的对象，避免关闭未被引用的
        临时对象。
        """

        if not package_version_id:
            raise ValueError("package_version_id 不能为空")
        loser = None
        with self._lock:
            if self.state is not RuntimeGenerationState.ACTIVE:
                raise RuntimeGenerationStateError(
                    f"Generation '{self.generation_id}' 当前为 {self.state.value}，"
                    "不能缓存 LoadedAgentPackage"
                )
            cached = self.loaded_packages.get(package_version_id)
            if cached is not None:
                loser = package if cached is not package else None
                result = cached
            else:
                self.loaded_packages[package_version_id] = package
                self._package_ref_counts[package_version_id] = 0
                result = package
        if loser is not None:
            self._schedule_resource_close(loser)
        return result

    def publish(self) -> None:
        """原子地发布完整 Generation。"""

        with self._lock:
            self._require_state(RuntimeGenerationState.BUILDING, "发布 Generation")
            # 先检查全部实例，确保一个坏实例不会让 Generation 半激活。
            for instance in self.extension_instances.values():
                if instance.state is not ExtensionInstanceState.LOADING:
                    raise RuntimeGenerationStateError(
                        f"Extension '{instance.extension_id}' 尚未处于 loading 状态"
                    )
            for instance in self.extension_instances.values():
                instance.activate()
            self.state = RuntimeGenerationState.ACTIVE

    def acquire_loaded_package(
        self,
        package_version_id: PackageVersionId,
    ) -> LoadedPackageHandle:
        """获取缓存 Package 的精确引用；只从 active Generation 接受新引用。"""

        with self._lock:
            self._require_state(
                RuntimeGenerationState.ACTIVE,
                "获取 LoadedAgentPackage 引用",
            )
            try:
                package = self.loaded_packages[package_version_id]
            except KeyError as exc:
                raise KeyError(
                    f"Generation '{self.generation_id}' 没有 Package "
                    f"'{package_version_id}'"
                ) from exc
            self._package_ref_counts[package_version_id] += 1
            self.operation_ref_count += 1
        return LoadedPackageHandle(self, package_version_id, package)

    def acquire_loaded_package_for_background(
        self, package_version_id: PackageVersionId
    ) -> LoadedPackageHandle:
        """为短暂的后台辅助任务保留已缓存 Package。

        标题任务可能在 reload 后继续使用旧 Generation 的冻结 Package；它
        不能借用同名新包，也不能让 retired Generation 在 lease 期间关闭。
        """
        with self._lock:
            if self.state not in {
                RuntimeGenerationState.ACTIVE,
                RuntimeGenerationState.RETIRED,
            }:
                raise RuntimeGenerationStateError(
                    f"Generation '{self.generation_id}' 当前为 {self.state.value}，"
                    "不能获取后台 Package 引用"
                )
            package = self.loaded_packages.get(package_version_id)
            if package is None:
                raise KeyError(
                    f"Generation '{self.generation_id}' 没有 Package "
                    f"'{package_version_id}'"
                )
            self._package_ref_counts[package_version_id] += 1
            self.operation_ref_count += 1
        return LoadedPackageHandle(self, package_version_id, package)

    def retire(self) -> None:
        """停止接受新引用，保留已有 Operation 直到其终态。"""

        with self._lock:
            self._require_state(RuntimeGenerationState.ACTIVE, "retire Generation")
            self.state = RuntimeGenerationState.RETIRED
            should_schedule = self.can_close
        if should_schedule:
            self._schedule_close()

    async def close(self) -> None:
        """关闭无引用的 retired Generation；重复调用无副作用。"""

        task = self._begin_close()
        if task is None:
            return
        await asyncio.shield(task)

    def _begin_close(self) -> asyncio.Task[None] | None:
        with self._lock:
            if self.state is RuntimeGenerationState.CLOSED:
                return None
            if self.state is RuntimeGenerationState.BUILDING:
                # 构建失败时允许直接回滚；它从未对外发布。
                self.state = RuntimeGenerationState.RETIRED
            elif self.state is not RuntimeGenerationState.RETIRED:
                raise RuntimeGenerationStateError(
                    f"Generation '{self.generation_id}' 不能从 {self.state.value} 关闭"
                )
            if self.operation_ref_count:
                raise RuntimeGenerationStateError(
                    f"Generation '{self.generation_id}' 仍有引用："
                    f"operations={self.operation_ref_count}"
                )
            task = self._close_task
            if task is None:
                task = asyncio.create_task(self._close_resources())
                self._close_task = task
            return task

    async def _close_resources(self) -> None:
        # 独立任务完成全部清理后才发布 closed；单个资源异常不阻止后续清理。
        for instance in reversed(tuple(self.extension_instances.values())):
            await instance.close()
        for package_id, package in tuple(self.loaded_packages.items()):
            try:
                await _close_resource(package)
            except Exception:
                logger.exception(
                    "关闭 Generation '%s' 的 Package '%s' 失败",
                    self.generation_id,
                    package_id,
                )
        try:
            await _close_resource(self.scope)
        except Exception:
            logger.exception("关闭 RuntimeGeneration '%s' 失败", self.generation_id)
        finally:
            with self._lock:
                self.state = RuntimeGenerationState.CLOSED

    async def wait_closed(self) -> None:
        """等待 retire 后已经安排的异步关闭，供 reload/shutdown 收口。"""

        task = self._close_task
        if task is not None:
            await asyncio.shield(task)

    async def _release_package(self, package_version_id: PackageVersionId) -> None:
        with self._lock:
            count = self._package_ref_counts.get(package_version_id, 0)
            if count <= 0:
                raise RuntimeGenerationStateError(
                    f"Package '{package_version_id}' 引用计数已经为零"
                )
            self._package_ref_counts[package_version_id] = count - 1
            self.operation_ref_count -= 1
            should_close = self.can_close
        if should_close:
            await self.close()

    def _release_package_sync(self, package_version_id: PackageVersionId) -> None:
        with self._lock:
            count = self._package_ref_counts.get(package_version_id, 0)
            if count <= 0:
                raise RuntimeGenerationStateError(
                    f"Package '{package_version_id}' 引用计数已经为零"
                )
            self._package_ref_counts[package_version_id] = count - 1
            self.operation_ref_count -= 1
            should_close = self.can_close
        if should_close:
            self._schedule_close()

    def _require_state(self, expected: RuntimeGenerationState, action: str) -> None:
        if self.state is not expected:
            raise RuntimeGenerationStateError(
                f"Generation '{self.generation_id}' 当前为 {self.state.value}，"
                f"不能{action}"
            )

    def _schedule_close(self) -> None:
        """retire 无引用时在当前事件循环尽快完成异步清理。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._close_task is None:
            try:
                self._begin_close()
            except RuntimeGenerationStateError:
                logger.exception(
                    "安排 RuntimeGeneration '%s' 关闭失败", self.generation_id
                )

    @staticmethod
    def _schedule_resource_close(resource: Any) -> None:
        """缓存竞争失败的临时 Package 也必须释放其 Provider client。"""

        close = getattr(resource, "close", None)
        if close is None:
            close = getattr(resource, "aclose", None)
        if close is None:
            return
        try:
            result = close()
        except Exception:
            logger.exception("关闭缓存竞争失败的 Package 失败")
            return
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # cache_loaded_package normally runs from an async Runtime path. If
            # a synchronous caller supplies an async-only resource, close it in
            # a short-lived loop rather than silently leaking it.
            asyncio.run(result)
        else:
            loop.create_task(result)


__all__ = [
    "ContributionScopeProtocol",
    "ExtensionInstance",
    "ExtensionInstanceState",
    "LoadedPackageHandle",
    "RuntimeGeneration",
    "RuntimeGenerationId",
    "RuntimeGenerationState",
    "RuntimeGenerationStateError",
]
