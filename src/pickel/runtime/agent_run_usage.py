"""AgentRun 的只读模型用量投影。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.conversation_node import ConversationNode


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


def project_agent_run_usage(
    nodes: Sequence[ConversationNode], input_node_id: str
) -> AgentRunUsage:
    """从一条已确定的 ConversationNode 分支投影 AgentRun 用量。

    ``nodes`` 必须是调用方已经捕获的根到明确终点的单条路径。输入节点本身
    不属于本次 Operation 的输出范围，因此只统计它之后的严格后代。
    该函数只消费传入值，不读取 Store，也不执行其他 Runtime 行为。
    """
    input_index = next(
        (index for index, node in enumerate(nodes) if node.node_id == input_node_id),
        None,
    )
    if input_index is None:
        raise ValueError(f"input_node_id 不在 branch nodes 中: {input_node_id}")

    steps = 0
    input_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    output_tokens = 0
    elapsed_ms = 0
    hook_injected_chars = 0
    labels: set[str] = set()

    for node in nodes[input_index + 1 :]:
        if node.content_type != "agent_message" or not isinstance(
            node.content, AssistantMessage
        ):
            continue

        steps += 1
        metadata = node.content.metadata
        if metadata is None:
            continue

        usage = metadata.usage
        if usage is not None:
            input_tokens += usage.input_tokens or 0
            cache_read_tokens += usage.cache_read_tokens or 0
            cache_write_tokens += usage.cache_write_tokens or 0
            output_tokens += usage.output_tokens or 0
        elapsed_ms += metadata.elapsed_ms or 0
        hook_injected_chars += metadata.hook_injected_chars or 0

        if metadata.provider and metadata.model:
            labels.add(f"{metadata.provider} / {metadata.model}")

    return AgentRunUsage(
        steps=steps,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        elapsed_ms=elapsed_ms,
        hook_injected_chars=hook_injected_chars,
        model_label=next(iter(labels)) if len(labels) == 1 else None,
    )
