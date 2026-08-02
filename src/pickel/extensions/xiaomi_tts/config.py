"""小米自动语音配置。"""

from pydantic import BaseModel, Field


class XiaomiTtsConfig(BaseModel):
    enabled: bool = True
    voice: str = "冰糖"
    style: str = "根据正文自然判断情绪，表达清晰自然。"
    max_text_chars: int = Field(default=4000, ge=1, le=20000)
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_attempts: int = Field(default=2, ge=1, le=5)
