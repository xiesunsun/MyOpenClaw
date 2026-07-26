"""skill 内容护栏：写入前的静态扫描。

定位同 S1 的危险命令拦截——挡「模型无意中写出危险 skill」，
不挡蓄意对抗（正则绕得过）。误杀比漏杀更难被发现，所以规则要求
「危险信号同时出现」而非单个关键词命中。
"""

from __future__ import annotations

import re

_CREDENTIAL_PATHS = r"~/\.ssh|~/\.aws|\.env\b|id_rsa|credentials\.json|\.kube/config"
_READ_ACTIONS = r"\bcat\b|\bless\b|\bopen\(|\bread_text\(|\bread\(\)"
_SECRET_VARS = r"\$\{?[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*\}?"
_UPLOAD_ACTIONS = r"\bcurl\b|\bwget\b|requests\.(?:post|put)|httpx\.(?:post|put)"


class SkillGuardError(Exception):
    def __init__(self, rule: str, excerpt: str) -> None:
        super().__init__(f"Blocked by skill guard ({rule}): {excerpt}")
        self.rule = rule
        self.excerpt = excerpt


def scan_skill_content(content: str) -> tuple[str, str] | None:
    """命中返回 (规则名, 匹配片段)；干净内容返回 None。"""
    for rule, matcher in (
        ("credential-harvesting", _credential_harvesting),
        ("credential-exfiltration", _credential_exfiltration),
        ("prompt-injection", _prompt_injection),
        ("sandbox-escape", _sandbox_escape),
    ):
        excerpt = matcher(content)
        if excerpt is not None:
            return rule, excerpt
    return None


def _credential_harvesting(content: str) -> str | None:
    # 凭据路径与读取动作同现于一行才算命中，避免误杀「需要 GEMINI_API_KEY」这类说明
    return _line_with_both(content, _CREDENTIAL_PATHS, _READ_ACTIONS)


def _credential_exfiltration(content: str) -> str | None:
    return _line_with_both(content, _UPLOAD_ACTIONS, _SECRET_VARS)


def _prompt_injection(content: str) -> str | None:
    pattern = re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"
        r"|disregard\s+(?:your\s+)?(?:system\s+)?prompt"
        r"|忽略(?:之前|以上|先前)(?:的)?(?:所有)?指令",
        re.IGNORECASE,
    )
    match = pattern.search(content)
    return match.group(0) if match else None


def _sandbox_escape(content: str) -> str | None:
    pattern = re.compile(
        r"sandbox\.allow_disable"
        r"|dangerously_disable_sandbox"
        r"|apparmor_restrict_unprivileged_userns"
        r"|/etc/apparmor\.d",
        re.IGNORECASE,
    )
    match = pattern.search(content)
    return match.group(0) if match else None


def _line_with_both(content: str, first: str, second: str) -> str | None:
    first_re = re.compile(first, re.IGNORECASE)
    second_re = re.compile(second, re.IGNORECASE)
    for line in content.splitlines():
        if first_re.search(line) and second_re.search(line):
            return line.strip()[:120]
    return None
