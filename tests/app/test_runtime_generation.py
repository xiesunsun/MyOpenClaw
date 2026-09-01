"""RuntimeGeneration 的生命周期与精确引用合同。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pickel.app.runtime_generation import (
    ExtensionInstance,
    ExtensionInstanceState,
    RuntimeGeneration,
    RuntimeGenerationState,
    RuntimeGenerationStateError,
)
from pickel.agents.agent_package import LoadedAgentPackage


class _Scope:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def close(self) -> None:
        self.events.append(self.name)


class _Package:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("package")


def test_publish_atomically_activates_loading_extensions() -> None:
    events: list[str] = []
    generation = RuntimeGeneration("generation-1", scope=_Scope("generation", events))
    extension = ExtensionInstance(
        "extension-1",
        "generation-1",
        _Scope("extension", events),
    )
    generation.add_extension(extension)

    assert generation.state is RuntimeGenerationState.BUILDING
    assert extension.state is ExtensionInstanceState.LOADING

    generation.publish()

    assert generation.state is RuntimeGenerationState.ACTIVE
    assert extension.state is ExtensionInstanceState.ACTIVE


def test_publish_rejects_half_active_extension_without_changing_generation() -> None:
    generation = RuntimeGeneration("generation-1")
    extension = ExtensionInstance(
        "extension-1",
        "generation-1",
        state=ExtensionInstanceState.ACTIVE,
    )
    generation.add_extension(extension)

    with pytest.raises(RuntimeGenerationStateError):
        generation.publish()

    assert generation.state is RuntimeGenerationState.BUILDING
    assert extension.state is ExtensionInstanceState.ACTIVE


def test_retired_generation_stays_alive_until_loaded_package_handle_closes() -> None:
    events: list[str] = []
    package = _Package(events)
    generation = RuntimeGeneration("generation-1", scope=_Scope("generation", events))
    generation.add_loaded_package("package-1", package)
    generation.publish()

    package_handle = generation.acquire_loaded_package("package-1")
    generation.retire()

    assert generation.state is RuntimeGenerationState.RETIRED
    assert generation.operation_ref_count == 1
    assert events == []

    asyncio.run(package_handle.close())

    assert generation.state is RuntimeGenerationState.CLOSED
    assert events == ["package", "generation"]


def test_old_handle_is_exact_and_close_is_idempotent() -> None:
    events: list[str] = []
    generation = RuntimeGeneration("generation-1", scope=_Scope("generation", events))
    generation.add_extension(
        ExtensionInstance("extension-1", "generation-1", _Scope("extension", events))
    )
    generation.add_loaded_package("package-1", object())
    generation.publish()
    package_handle = generation.acquire_loaded_package("package-1")
    generation.retire()

    asyncio.run(package_handle.close())
    asyncio.run(package_handle.close())
    asyncio.run(generation.close())
    asyncio.run(generation.close())

    assert generation.state is RuntimeGenerationState.CLOSED
    assert events == ["extension", "generation"]


def test_generation_cleanup_survives_cancelled_caller() -> None:
    class BlockingScope:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def close(self) -> None:
            self.calls += 1
            self.started.set()
            await self.release.wait()

    class Package:
        def __init__(self, events: list[str]) -> None:
            self.events = events

        async def close(self) -> None:
            self.events.append("package")

    async def scenario() -> tuple[RuntimeGeneration, BlockingScope, list[str]]:
        events: list[str] = []
        scope = BlockingScope()
        generation = RuntimeGeneration("generation-1", scope=scope)
        generation.add_loaded_package("package-1", Package(events))

        closing = asyncio.create_task(generation.close())
        await scope.started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert generation.state is not RuntimeGenerationState.CLOSED
        scope.release.set()
        await generation.close()
        return generation, scope, events

    generation, scope, events = asyncio.run(scenario())

    assert generation.closed
    assert scope.calls == 1
    assert events == ["package"]


def test_retired_generation_rejects_new_package_references() -> None:
    generation = RuntimeGeneration("generation-1")
    generation.add_loaded_package("package-1", object())
    generation.publish()
    generation.retire()

    with pytest.raises(RuntimeGenerationStateError):
        generation.acquire_loaded_package("package-1")


def test_extension_close_is_idempotent_even_when_scope_reports_failure() -> None:
    class BrokenScope:
        def __init__(self) -> None:
            self.calls = 0

        async def close(self) -> None:
            self.calls += 1
            raise RuntimeError("cleanup failed")

    scope = BrokenScope()
    extension = ExtensionInstance("extension-1", "generation-1", scope)

    asyncio.run(extension.close())
    asyncio.run(extension.close())

    assert extension.state is ExtensionInstanceState.CLOSED
    assert scope.calls == 1


def test_extension_cleanup_survives_cancelled_caller() -> None:
    class BlockingScope:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def close(self) -> None:
            self.calls += 1
            self.started.set()
            await self.release.wait()

    async def scenario() -> tuple[ExtensionInstance, BlockingScope]:
        scope = BlockingScope()
        extension = ExtensionInstance("extension-1", "generation-1", scope)
        closing = asyncio.create_task(extension.close())
        await scope.started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        scope.release.set()
        await extension.close()
        return extension, scope

    extension, scope = asyncio.run(scenario())

    assert extension.state is ExtensionInstanceState.CLOSED
    assert scope.calls == 1


def test_loaded_package_closes_provider_clients_and_isolates_resource_errors() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1

    class Provider:
        def __init__(self, client) -> None:
            self.client = client

    class Broken:
        async def close(self) -> None:
            raise RuntimeError("broken resource")

    client = Client()
    package = LoadedAgentPackage(
        version=SimpleNamespace(
            package_version_id="agentpkg_" + "a" * 64,
            runtime_policy=SimpleNamespace(max_parallel_model_requests=1),
        ),
        model_clients={"primary": Provider(client), "worker": Provider(client)},
        tool_snapshot=object(),
        lifecycle_hooks=(Broken(),),
    )

    asyncio.run(package.close())
    asyncio.run(package.close())

    assert package.closed
    assert client.calls == 1


def test_loaded_package_cleanup_survives_cancelled_caller() -> None:
    class Client:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def aclose(self) -> None:
            self.started.set()
            await self.release.wait()
            self.calls += 1

    client = Client()
    package = LoadedAgentPackage(
        version=SimpleNamespace(
            package_version_id="agentpkg_" + "b" * 64,
            runtime_policy=SimpleNamespace(max_parallel_model_requests=1),
        ),
        model_clients={"primary": SimpleNamespace(client=client)},
        tool_snapshot=object(),
    )

    async def scenario() -> None:
        closing = asyncio.create_task(package.close())
        await client.started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not package.closed
        client.release.set()
        await package.close()

    asyncio.run(scenario())

    assert package.closed
    assert client.calls == 1


def test_cache_loser_package_is_closed() -> None:
    class Package:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    winner = Package()
    loser = Package()
    generation = RuntimeGeneration("generation-1")
    generation.publish()
    generation.cache_loaded_package("package-1", winner)

    assert generation.cache_loaded_package("package-1", loser) is winner
    asyncio.run(asyncio.sleep(0))

    assert loser.closed
