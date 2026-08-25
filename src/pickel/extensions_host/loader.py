"""Extension 发现与装载。

发现顺序：内置（pickel.extensions.*）→ 用户级（~/.pickel/extensions/*）。
同名时用户级覆盖内置 —— 允许用户就地替换内置实现。
任一 extension 装载失败都被隔离：记错、回滚它注册的工具、继续装其余。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import pkgutil
from types import ModuleType
from typing import Any

from pickel.agents.agent_package import ExtensionVersion, ImplementationRef
from pickel.agents.agent_package_builder import sanitize_extension_config
from pickel.config.paths import home_dir
from pickel.extensions_host.errors import ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus

logger = logging.getLogger(__name__)

_BUILTIN_PACKAGE = "pickel.extensions"
_USER_DIR_NAME = "extensions"


@dataclass
class LoadResult:
    registry: ExtensionRegistry = field(default_factory=ExtensionRegistry)
    errors: list[ExtensionLoadError] = field(default_factory=list)
    modules: dict[str, ModuleType] = field(default_factory=dict)
    hosts: dict[str, ExtensionHost] = field(default_factory=dict)
    extension_versions: dict[str, ExtensionVersion] = field(default_factory=dict)


def load_extensions(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
    builtin_package: str | None = _BUILTIN_PACKAGE,
    enabled_names: Collection[str] | None = None,
) -> LoadResult:
    """同步入口。setup 可以是 async def，内部 asyncio.run 承接。

    builtin_package=None 可关掉内置发现（测试隔离用）。
    """
    return asyncio.run(
        load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            home=home,
            builtin_package=builtin_package,
            enabled_names=enabled_names,
        )
    )


async def load_extensions_async(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
    builtin_package: str | None = _BUILTIN_PACKAGE,
    enabled_names: Collection[str] | None = None,
) -> LoadResult:
    result = LoadResult()
    sections = getattr(app_config, "extensions", None) or {}
    discovered = _discover(home, builtin_package)
    if enabled_names is not None:
        requested = set(enabled_names)
        missing = requested.difference(discovered)
        for name in sorted(missing):
            result.errors.append(
                ExtensionLoadError(f"Unknown extension requested by agent: '{name}'")
            )
        discovered = {
            name: loader for name, loader in discovered.items() if name in requested
        }

    for name, module_loader in discovered.items():
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
                ExtensionLoadError(f"Extension '{name}' has no setup(host) function")
            )
            continue

        try:
            extension_version = extension_version_for_module(
                name, module, sections.get(name)
            )
        except Exception as exc:
            result.errors.append(
                ExtensionLoadError(
                    f"Failed to snapshot extension '{name}' implementation: {exc}"
                )
            )
            logger.exception("Failed to snapshot extension '%s'", name)
            continue

        host = ExtensionHost(
            name=name,
            config_section=sections.get(name),
            tool_bus=tool_bus,
            registry=result.registry,
            app_config=app_config,
            defer_publish=True,
            extension_version=extension_version,
        )
        try:
            outcome = setup(host)
            if inspect.isawaitable(outcome):
                await outcome
            host.publish()
        except Exception as exc:
            # setup rollback 与正常卸载统一走精确 Scope.close()。
            await host.scope.close()
            result.errors.append(
                ExtensionLoadError(f"Extension '{name}' setup failed: {exc}")
            )
            logger.exception("Extension '%s' setup failed", name)
            continue

        result.modules[name] = module
        result.hosts[name] = host
        result.extension_versions[name] = extension_version

    return result


async def teardown_extensions(result: LoadResult, *, tool_bus: ToolBus) -> None:
    """卸载所有 Extension；每个实例只通过自己的 Scope 精确清理。"""
    for name in reversed(tuple(result.modules)):
        host = result.hosts.get(name)
        if host is not None:
            await host.scope.close()


def _discover(home: Path | None, builtin_package: str | None) -> dict[str, Any]:
    """名字 → 返回模块的可调用。用户级同名覆盖内置。"""
    found: dict[str, Any] = {}

    if builtin_package is not None:
        for name in _iter_builtin_module_names(builtin_package):
            found[name] = lambda n=name, pkg=builtin_package: importlib.import_module(
                f"{pkg}.{n}"
            )

    user_root = (home or home_dir()) / _USER_DIR_NAME
    for path in _iter_user_paths(user_root):
        name = path.stem if path.is_file() else path.name
        found[name] = lambda p=path, n=name: _import_from_path(n, p)

    return dict(sorted(found.items()))


def _iter_builtin_module_names(builtin_package: str) -> list[str]:
    try:
        package = importlib.import_module(builtin_package)
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


def extension_version_for_module(
    extension_id: str,
    module: ModuleType,
    config_section: Any,
) -> ExtensionVersion:
    """捕获当前实际模块的 ExtensionVersion。

    digest 只覆盖模块源文件（包则覆盖包内所有 ``.py`` 文件），并使用相对
    路径和文件内容组成规范输入，因此不受临时目录、mtime 或 pycache 影响。
    """
    if not extension_id:
        raise ValueError("extension_id 不能为空")
    config, refs = sanitize_extension_config(config_section or {}, extension_id)
    version = _declared_version(module)
    return ExtensionVersion(
        extension_id=extension_id,
        implementation_ref=ImplementationRef(
            "extension",
            extension_id,
            version=version,
            digest=_module_source_digest(module),
        ),
        version=version,
        config=config,
        required_secret_refs=refs,
    )


def _declared_version(module: ModuleType) -> str | None:
    """读取模块公开声明的版本；未声明时保持 None。"""
    for name in ("__version__", "EXTENSION_VERSION", "VERSION"):
        value = getattr(module, name, None)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{name} 必须是非空字符串")
        return value.strip()
    return None


def _module_source_digest(module: ModuleType) -> str:
    files, root = _module_source_files(module)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _module_source_files(module: ModuleType) -> tuple[tuple[Path, ...], Path]:
    raw_file = getattr(module, "__file__", None)
    if not raw_file:
        raise ValueError("模块没有 __file__，无法生成实现 digest")
    module_file = Path(raw_file).resolve()
    if module_file.suffix == ".pyc":
        source = module_file.with_suffix(".py")
        if source.is_file():
            module_file = source
    if not module_file.is_file():
        raise ValueError(f"模块源文件不存在: {module_file}")
    if module_file.name == "__init__.py":
        root = module_file.parent
        files = tuple(
            sorted(root.rglob("*.py"), key=lambda path: path.relative_to(root))
        )
    else:
        root = module_file.parent
        files = (module_file,)
    if not files:
        raise ValueError(f"模块没有可哈希的 Python 源文件: {module_file}")
    return files, root
