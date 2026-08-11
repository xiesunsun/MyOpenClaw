"""AgentRun 的只读模型用量投影。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRunUsage:
    """一段区间内所有模型调用的真实用量合计。"""

    steps: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    hook_injected_chars: int = 0
    model_label: str | None = None

    @property
    def actual_input_tokens(self) -> int:
        """实际输入规模（§5.1）。

        Anthropic 的 input_tokens 不含 cache，单独展示会低估一个数量级。
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
