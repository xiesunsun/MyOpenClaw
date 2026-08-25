"""RuntimeGeneration 的生命周期与精确引用合同。"""

from __future__ import annotations

import asyncio

import pytest

from pickel.app.runtime_generation import (
    ExtensionInstance,
    ExtensionInstanceState,
    RuntimeGeneration,
    RuntimeGenerationState,
    RuntimeGenerationStateError,
)


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
