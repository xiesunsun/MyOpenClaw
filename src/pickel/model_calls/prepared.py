"""Provider Mapper 产出的不可变真实 wire 请求。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pickel.shared.frozen_json import FrozenJSON, freeze_json_object


@dataclass(frozen=True)
class PreparedModelCall:
    """一次即将发送的完整 Provider wire request。"""

    provider: str
    api_kind: str
    endpoint: str
    requested_model: str
    body: Mapping[str, FrozenJSON]

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("api_kind", self.api_kind),
            ("endpoint", self.endpoint),
            ("requested_model", self.requested_model),
        ):
            if not value:
                raise ValueError(f"PreparedModelCall.{name} 不能为空")
        object.__setattr__(self, "body", freeze_json_object(self.body))
