#!/usr/bin/env python3
"""
Skill Initializer - Creates a new skill from template

Usage:
    init_skill.py <skill-name> --path <path> [--resources scripts,references,assets] [--examples]

Examples:
    init_skill.py my-new-skill --path skills/
    init_skill.py my-new-skill --path skills/ --resources scripts,references
    init_skill.py my-api-helper --path skills/ --resources scripts --examples
"""

import argparse
import re
import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCES = {"scripts", "references", "assets"}

SKILL_TEMPLATE = """---
name: {skill_name}
description: [TODO: 清晰说明此 Skill 能做什么，以及何时应该触发它。包含具体场景、文件类型、触发词等。]
---

# {skill_title}

## 概述

[TODO: 1-2 句话说明此 Skill 的用途]

## [TODO: 根据 Skill 类型选择合适的结构]

[常用结构模式：
1. 工作流型 — 有明确步骤顺序时（如：决策树 -> 步骤1 -> 步骤2）
2. 任务型   — 提供多种操作时（如：快速开始 -> 合并 -> 拆分 -> 提取）
3. 参考型   — 规范或标准时（如：规则 -> 规格 -> 示例）
删除此注释后开始编写实际内容。]

"""

EXAMPLE_SCRIPT = '''#!/usr/bin/env python3
"""
{skill_name} 的示例辅助脚本

替换为实际实现，或删除此文件。
"""

def main():
    print("示例脚本：{skill_name}")
    # TODO: 添加实际逻辑

if __name__ == "__main__":
    main()
'''

EXAMPLE_REFERENCE = """# {skill_title} 参考文档

替换为实际参考内容，或删除此文件。

## 适合放入 references/ 的内容
- API 文档
- 数据库 Schema
- 详细工作流指南
- 业务规则和策略
- 超出 SKILL.md 篇幅的详细信息
"""

EXAMPLE_ASSET = """# 示例资产文件占位符

替换为实际资产文件（模板、图片、字体等），或删除此文件。

assets/ 中的文件不会加载到上下文，而是直接用于输出内容。
常见类型：.pptx/.docx 模板、图片、字体、样板代码目录等。
"""


def normalize_skill_name(skill_name):
    normalized = skill_name.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def title_case_skill_name(skill_name):
    return " ".join(word.capitalize() for word in skill_name.split("-"))


def parse_resources(raw_resources):
    if not raw_resources:
        return []
    resources = [item.strip() for item in raw_resources.split(",") if item.strip()]
    invalid = sorted({item for item in resources if item not in ALLOWED_RESOURCES})
    if invalid:
        allowed = ", ".join(sorted(ALLOWED_RESOURCES))
        print(f"[ERROR] 未知资源类型: {', '.join(invalid)}")
        print(f"   允许的类型: {allowed}")
        sys.exit(1)
    deduped = []
    seen = set()
    for resource in resources:
        if resource not in seen:
            deduped.append(resource)
            seen.add(resource)
    return deduped


def create_resource_dirs(
    skill_dir, skill_name, skill_title, resources, include_examples
):
    for resource in resources:
        resource_dir = skill_dir / resource
        resource_dir.mkdir(exist_ok=True)
        if resource == "scripts":
            if include_examples:
                example_script = resource_dir / "example.py"
                example_script.write_text(EXAMPLE_SCRIPT.format(skill_name=skill_name))
                example_script.chmod(0o755)
                print("[OK] 创建 scripts/example.py")
            else:
                print("[OK] 创建 scripts/")
        elif resource == "references":
            if include_examples:
                example_ref = resource_dir / "reference.md"
                example_ref.write_text(
                    EXAMPLE_REFERENCE.format(skill_title=skill_title)
                )
                print("[OK] 创建 references/reference.md")
            else:
                print("[OK] 创建 references/")
        elif resource == "assets":
            if include_examples:
                example_asset = resource_dir / "example_asset.txt"
                example_asset.write_text(EXAMPLE_ASSET)
                print("[OK] 创建 assets/example_asset.txt")
            else:
                print("[OK] 创建 assets/")


def init_skill(skill_name, path, resources, include_examples):
    skill_dir = Path(path).resolve() / skill_name

    if skill_dir.exists():
        print(f"[ERROR] Skill 目录已存在: {skill_dir}")
        return None

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        print(f"[OK] 创建目录: {skill_dir}")
    except Exception as e:
        print(f"[ERROR] 创建目录失败: {e}")
        return None

    skill_title = title_case_skill_name(skill_name)
    skill_content = SKILL_TEMPLATE.format(
        skill_name=skill_name, skill_title=skill_title
    )

    skill_md_path = skill_dir / "SKILL.md"
    try:
        skill_md_path.write_text(skill_content)
        print("[OK] 创建 SKILL.md")
    except Exception as e:
        print(f"[ERROR] 创建 SKILL.md 失败: {e}")
        return None

    if resources:
        try:
            create_resource_dirs(
                skill_dir, skill_name, skill_title, resources, include_examples
            )
        except Exception as e:
            print(f"[ERROR] 创建资源目录失败: {e}")
            return None

    print(f"\n[OK] Skill '{skill_name}' 初始化成功: {skill_dir}")
    print("\n后续步骤:")
    print("1. 编辑 SKILL.md，补全 TODO 项，完善 description 触发说明")
    if resources:
        if include_examples:
            print("2. 定制或删除示例文件（scripts/、references/、assets/）")
        else:
            print("2. 按需向 scripts/、references/、assets/ 中添加资源")
    else:
        print("2. 按需创建资源目录（scripts/、references/、assets/）")
    print("3. 完成后运行 quick_validate.py 检查结构")

    return skill_dir


def main():
    parser = argparse.ArgumentParser(description="创建新的 Skill 目录和模板文件。")
    parser.add_argument("skill_name", help="Skill 名称（自动规范化为 hyphen-case）")
    parser.add_argument("--path", required=True, help="Skill 的输出目录")
    parser.add_argument(
        "--resources",
        default="",
        help="逗号分隔的资源类型: scripts,references,assets",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="在资源目录中创建示例文件",
    )
    args = parser.parse_args()

    raw_skill_name = args.skill_name
    skill_name = normalize_skill_name(raw_skill_name)

    if not skill_name:
        print("[ERROR] Skill 名称必须包含至少一个字母或数字。")
        sys.exit(1)
    if len(skill_name) > MAX_SKILL_NAME_LENGTH:
        print(
            f"[ERROR] Skill 名称过长 ({len(skill_name)} 字符)，最大允许 {MAX_SKILL_NAME_LENGTH} 字符。"
        )
        sys.exit(1)
    if skill_name != raw_skill_name:
        print(f"注意: Skill 名称已规范化 '{raw_skill_name}' -> '{skill_name}'")

    resources = parse_resources(args.resources)
    if args.examples and not resources:
        print("[ERROR] --examples 需要同时指定 --resources。")
        sys.exit(1)

    print(f"初始化 Skill: {skill_name}")
    print(f"   位置: {args.path}")
    if resources:
        print(f"   资源: {', '.join(resources)}")
        if args.examples:
            print("   示例文件: 启用")
    else:
        print("   资源: 无（按需创建）")
    print()

    result = init_skill(skill_name, args.path, resources, args.examples)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
