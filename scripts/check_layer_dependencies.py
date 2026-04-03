#!/usr/bin/env python3

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = PROJECT_ROOT / "src" / "myopenclaw"


PACKAGE_LAYERS: dict[str, str] = {
    "shared": "shared",
    "domain": "domain",
    "application": "application",
    "infrastructure": "infrastructure",
    "interfaces": "interfaces",
    "bootstrap": "bootstrap",
}

LAYER_RULES: dict[str, set[str]] = {
    "shared": set(),
    "domain": {"shared"},
    "application": {"domain", "shared"},
    "infrastructure": {"application", "domain", "shared"},
    "interfaces": {"application", "shared"},
    "bootstrap": {"interfaces", "application", "infrastructure", "domain", "shared"},
}


@dataclass(frozen=True)
class Violation:
    file_path: Path
    line_number: int
    source_package: str
    target_package: str

    def render(self, source_root: Path) -> str:
        relative_path = self.file_path.relative_to(source_root.parent)
        return (
            f"{relative_path}:{self.line_number}: "
            f"{self.source_package} must not depend on {self.target_package}"
        )


def package_name_for_file(file_path: Path, source_root: Path) -> str | None:
    relative = file_path.relative_to(source_root)
    if len(relative.parts) < 2:
        return None
    return relative.parts[0]


def module_parts_for_file(file_path: Path, source_root: Path) -> list[str]:
    relative = file_path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        return parts[:-1]
    return parts


def current_package_parts(file_path: Path, source_root: Path) -> list[str]:
    parts = module_parts_for_file(file_path, source_root)
    if file_path.name == "__init__.py":
        return parts
    return parts[:-1]


def resolve_target_package(
    *,
    file_path: Path,
    source_root: Path,
    node: ast.AST,
) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            if len(parts) >= 2 and parts[0] == "myopenclaw":
                return parts[1]
        return None

    if not isinstance(node, ast.ImportFrom):
        return None

    if node.level == 0:
        if node.module is None:
            return None
        parts = node.module.split(".")
        if len(parts) >= 2 and parts[0] == "myopenclaw":
            return parts[1]
        return None

    package_parts = current_package_parts(file_path, source_root)
    steps_up = node.level - 1
    if steps_up > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - steps_up]
    if node.module:
        base_parts.extend(node.module.split("."))
    if not base_parts:
        return None
    return base_parts[0]


def find_violations(source_root: Path = SOURCE_ROOT) -> list[Violation]:
    violations: list[Violation] = []

    for file_path in sorted(source_root.rglob("*.py")):
        source_package = package_name_for_file(file_path, source_root)
        if source_package is None or source_package not in PACKAGE_LAYERS:
            continue

        source_layer = PACKAGE_LAYERS[source_package]
        allowed_layers = LAYER_RULES[source_layer]
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            target_package = resolve_target_package(
                file_path=file_path,
                source_root=source_root,
                node=node,
            )
            if target_package is None or target_package == source_package:
                continue
            if target_package not in PACKAGE_LAYERS:
                continue
            target_layer = PACKAGE_LAYERS[target_package]
            if target_layer not in allowed_layers:
                violations.append(
                    Violation(
                        file_path=file_path,
                        line_number=getattr(node, "lineno", 1),
                        source_package=source_package,
                        target_package=target_package,
                    )
                )

    return violations


def main() -> int:
    violations = find_violations()
    if not violations:
        print("Layer dependency check passed.")
        return 0

    print("Layer dependency violations found:")
    for violation in violations:
        print(f"- {violation.render(SOURCE_ROOT)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
