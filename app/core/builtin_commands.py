# -*- coding: utf-8 -*-
"""兼容垫片 — 供第三方市场插件引用旧路径（实现已迁至 app/core/commands/builtin_commands.py）。

背景：market 插件（browser / quick-screenshot / workbuddy / ip-switcher）的 ui 层
`from app.core.builtin_commands import FunctionCommandHandlers`，主程序内部路径
重组无法同步已分发插件，故保留本垫片防加载失败。

移除条件：市场源头已改为新路径且插件用户完成更新后（观察本模块加载日志归零）。
新增主程序代码禁止 import 本模块 —— 统一走 app.core.commands.builtin_commands。
"""

from __future__ import annotations

from loguru import logger

logger.warning("[Compat] app.core.builtin_commands 为兼容垫片，请插件改用 app.core.commands.builtin_commands")

from app.core.commands.builtin_commands import *  # noqa: F401,F403
from app.core.commands.builtin_commands import FunctionCommandHandlers  # noqa: F401
