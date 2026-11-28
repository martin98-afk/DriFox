# -*- coding: utf-8 -*-
"""安全模式：仅加载系统插件的诊断启动通道。

由托盘菜单或首次使用引导的重启入口注入 ``--safe-mode`` 命令行开关生效。不带该开关
的任何启动方式（双击 / 开机自启 / 快捷方式）都是正常模式；开关只存在于本进程
的命令行参数中，不写任何持久化配置，退出后即失效。
"""

from __future__ import annotations

import sys
from typing import Iterable

SAFE_MODE_FLAG = "--safe-mode"

__all__ = ["SAFE_MODE_FLAG", "is_safe_mode"]


def is_safe_mode(argv: Iterable[str] | None = None) -> bool:
    """当前进程是否为安全模式（默认读 ``sys.argv[1:]``）"""
    args = sys.argv[1:] if argv is None else list(argv)
    return SAFE_MODE_FLAG in args
