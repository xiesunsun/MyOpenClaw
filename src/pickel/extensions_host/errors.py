"""Extension 宿主的错误类型。"""

from __future__ import annotations


class ExtensionLoadError(Exception):
    """extension 发现或装载失败（import 失败、缺 setup、setup 抛异常）。"""


class ExtensionConfigError(Exception):
    """extension 的配置段校验失败。"""
