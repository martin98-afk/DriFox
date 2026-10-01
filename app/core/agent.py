# -*- coding: utf-8 -*-
"""兼容垫片 — 供第三方市场插件引用旧路径（实现已迁至 app/core/conversation/agent.py）。

背景：market 插件 pixel-team-studio `from app.core.agent import AgentManager`，
主程序内部路径重组无法同步已分发插件，保留垫片防加载失败。

移除条件：市场源头修复发版且插件用户完成更新后。新增主程序代码禁止 import 本模块。
"""

from __future__ import annotations

from loguru import logger

logger.warning("[Compat] app.core.agent 为兼容垫片，请插件改用 app.core.conversation.agent")

from app.core.conversation.agent import *  # noqa: F401,F403
from app.core.conversation.agent import Agent, AgentManager, create_agent_manager  # noqa: F401
