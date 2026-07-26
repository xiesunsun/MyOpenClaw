"""模板加载：包默认 → 用户 home → 项目 .pickel，后层非空覆盖。"""

from __future__ import annotations

from pathlib import Path

from pickel.config.paths import home_dir

# 包内默认模板目录：src/pickel/templates
_PACKAGE_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _normalize_template_text(text: str) -> str:
    """去掉末尾空白/换行，不改动正文内部空白。"""
    return text.rstrip()


def _load_templates_from_dir(directory: Path) -> dict[str, str]:
    """从目录加载 *.md；键为文件名 stem，仅保留非空内容。"""
    if not directory.is_dir():
        return {}

    result: dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        text = _normalize_template_text(raw)
        if not text:
            continue
        result[path.stem] = text
    return result


def load_templates(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, str]:
    """加载模板字典。

    合并顺序（后者覆盖前者，仅当文件存在且非空）：
    1. 包默认 ``src/pickel/templates``
    2. ``{home}/templates``；home 缺省时用 ``home_dir()/templates``
    3. ``{project_root}/.pickel/templates``（若提供 project_root）
    """
    merged: dict[str, str] = {}
    merged.update(_load_templates_from_dir(_PACKAGE_TEMPLATES_DIR))

    home_base = Path(home) if home is not None else home_dir()
    merged.update(_load_templates_from_dir(home_base / "templates"))

    if project_root is not None:
        merged.update(
            _load_templates_from_dir(Path(project_root) / ".pickel" / "templates")
        )

    return merged
