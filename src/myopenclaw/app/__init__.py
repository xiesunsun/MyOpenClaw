"""Application bootstrap."""

from myopenclaw.app.assembly import AppAssembly
from myopenclaw.app.behavior_loader import BehaviorLoader

__all__ = [
    "AppAssembly",
    "BehaviorLoader",
]
