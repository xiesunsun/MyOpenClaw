"""Extension 发现与装载。

发现顺序：内置（pickel.extensions.*）→ 用户级（~/.pickel/extensions/*）。
同名时用户级覆盖内置 —— 允许用户就地替换内置实现。
任一 extension 装载失败都被隔离：记错、回滚它注册的工具、继续装其余。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import pkgutil
from types import ModuleType
from typing import Any

from pickel.config.paths import home_dir
from pickel.extensions_host.errors import ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus, ToolSource

logger = logging.getLogger(__name__)

_BUILTIN_PACKAGE = "pickel.extensions"
_USER_DIR_NAME = "extensions"


@dataclass
class LoadResult:
    registry: ExtensionRegistry = field(default_factory=ExtensionRegistry)
    errors: list[ExtensionLoadError] = field(default_factory=list)
    modules: dict[str, ModuleType] = field(default_factory=dict)


def load_extensions(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
) -> LoadResult:
    """同步入口。setup 可以是 async def，内部 asyncio.run 承接。"""
    return asyncio.run(
        load_extensions_async(tool_bus=tool_bus, app_config=app_config, home=home)
    )


async def load_extensions_async(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
) -> LoadResult:
    result = LoadResult()
    sections = getattr(app_config, "extensions", None) or {}

    for name, module_loader in _discover(home).items():
        try:
            module = module_loader()
        except Exception as exc:
            result.errors.append(
                ExtensionLoadError(f"Failed to import extension '{name}': {exc}")
            )
            logger.exception("Failed to import extension '%s'", name)
            continue

        setup = getattr(module, "setup", None)
        if setup is None:
            result.errors.append(
                ExtensionLoadError(
                    f"Extension '{name}' has no setup(host) function"
                )
            )
            continue

        host = ExtensionHost(
            name=name,
            config_section=sections.get(name),
            tool_bus=tool_bus,
            registry=result.registry,
        )
        try:
            outcome = setup(host)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            # 回滚半装状态：它可能已经注册了一部分工具
            tool_bus.unregister_origin(ToolSource.EXTENSION, name)
            result.errors.append(
                ExtensionLoadError(f"Extension '{name}' setup failed: {exc}")
            )
            logger.exception("Extension '%s' setup failed", name)
            continue

        result.modules[name] = module

    return result


async def teardown_extensions(result: LoadResult, *, tool_bus: ToolBus) -> None:
    """卸载：调各 extension 可选的 teardown，并摘掉它们注册的工具。"""
    for name, module in result.modules.items():
        teardown = getattr(module, "teardown", None)
        if teardown is not None:
            try:
                outcome = teardown()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                logger.exception("Extension '%s' teardown failed", name)
        tool_bus.unregister_origin(ToolSource.EXTENSION, name)


def _discover(home: Path | None) -> dict[str, Any]:
    """名字 → 返回模块的可调用。用户级同名覆盖内置。"""
    found: dict[str, Any] = {}

    for name in _iter_builtin_module_names():
        found[name] = lambda n=name: importlib.import_module(f"{_BUILTIN_PACKAGE}.{n}")

    user_root = (home or home_dir()) / _USER_DIR_NAME
    for path in _iter_user_paths(user_root):
        name = path.stem if path.is_file() else path.name
        found[name] = lambda p=path, n=name: _import_from_path(n, p)

    return dict(sorted(found.items()))


def _iter_builtin_module_names() -> list[str]:
    try:
        package = importlib.import_module(_BUILTIN_PACKAGE)
    except ModuleNotFoundError:
        return []
    return [
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    ]


def _iter_user_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(("_", ".")):
            continue
        if child.is_dir() and (child / "__init__.py").is_file():
            paths.append(child)
        elif child.is_file() and child.suffix == ".py":
            paths.append(child)
    return paths


def _import_from_path(name: str, path: Path) -> ModuleType:
    target = path / "__init__.py" if path.is_dir() else path
    # 模块名加前缀，避免与已导入的同名模块撞
    spec = importlib.util.spec_from_file_location(f"pickel_ext_{name}", target)
    if spec is None or spec.loader is None:
        raise ExtensionLoadError(f"Cannot load extension '{name}' from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
