from dataclasses import dataclass


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
