from dataclasses import dataclass


@dataclass
class MessageMetadata:
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: int | None = None
