"""LifecycleHooks 最小实现：无 handler 时恒 allow/continue。"""

from __future__ import annotations


class LifecycleHooks:
    """第一版空分发器；Task 9 接入真实事件。"""

    def __init__(self, handlers: list | None = None) -> None:
        self.handlers = list(handlers or [])


class NoopLifecycleHooks(LifecycleHooks):
    def __init__(self) -> None:
        super().__init__(handlers=[])
